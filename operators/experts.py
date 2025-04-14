#!/usr/bin/env python
# coding=utf-8
'''
Description  :  
Author       : Azure-Tang, Boxin Zhang, chenht2022
Date         : 2024-07-25 11:25:24
Version      : 0.1.0
LastEditors  : Azure 
LastEditTime : 2024-08-29 09:41:10
Copyright (c) 2024 by KVCache.AI, All Rights Reserved. 
'''

from typing import Any, Union
import numpy as np
import numpy.typing as npt
from torch import Tensor, nn
import torch.nn.functional as F
import torch
import sys, os
from ktransformers.operators.base_operator import BaseInjectedModule

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "ktransformers_ext", "build"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "ktransformers_ext", "build", "Release"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "ktransformers_ext", "build", "Debug"))
import cpuinfer_ext
from cpuinfer_ext.moe import MOEConfig, MOE
from cpuinfer_ext.moe import AMX_MOEConfig, AMXBF16_MOE, AMXInt8_MOE
import ctypes
from ktransformers.util.custom_gguf import GGMLQuantizationType, GGUFLoader, dequantize_q4_k, dequantize_q5_k, dequantize_q6_k
from ktransformers.util.utils import InferenceState
from ktransformers.server.config.config import Config
from transformers.activations import ACT2FN
from transformers.configuration_utils import PretrainedConfig
from abc import ABC, abstractmethod
from ktransformers.operators.linear import KLinearMarlin, KLinearTorch, KTransformersLinear
import time
from ktransformers.operators.cpuinfer import CPUInfer


# class Base(BaseInjectedModule, ABC):
class KExpertsBase(ABC):
    def __init__(self, key: str, gguf_loader: GGUFLoader, config: PretrainedConfig, orig_module: nn.Module, device: str = "cpu", **kwargs):
        # super().__init__(key, gguf_loader, config, orig_module, device, **kwargs)
        self.key = key
        self.gguf_loader = gguf_loader
        self.config = config
        self.device = device
    
    @abstractmethod
    def forward(self, input_tensor, expert_ids, weights):
        pass

    @abstractmethod
    def load(self, w: dict | nn.Parameter | tuple | None = None, device: str = "cpu"):
        pass
    
    @abstractmethod
    def unload():
        pass

    def load_weights(self, override_key: str | None = None, device: str = "cpu"):
        res = {}
        if override_key is not None:
            keys = override_key
        else:
            keys = [self.key]

        gate = None
        up = None
        down = None
        gate_type = None
        up_type = None
        down_type = None

        for key in keys:
            if key + ".ffn_gate_exps.weight" in self.gguf_loader.tensor_info:
                targets = [".ffn_gate_exps.weight", ".ffn_up_exps.weight", ".ffn_down_exps.weight" ]
                tensors = self.load_multi(key, targets, device=device)
                gate = tensors[".ffn_gate_exps.weight"]
                up = tensors[".ffn_up_exps.weight"]
                down = tensors[".ffn_down_exps.weight"]
                gate_type = self.gguf_loader.tensor_info[key + ".ffn_gate_exps.weight"]["ggml_type"]
                up_type = self.gguf_loader.tensor_info[key + ".ffn_up_exps.weight"]["ggml_type"]
                down_type = self.gguf_loader.tensor_info[key + ".ffn_down_exps.weight"]["ggml_type"]
            elif key + ".ffn_down.0.weight" in self.gguf_loader.tensor_info:
                # for supporting  Mixtral-8x7B-Instuct  
                gate = []
                up = []
                down = []
                for i in range(8):
                    gatei, upi, downi = f".ffn_gate.{i}.weight", f".ffn_up.{i}.weight", f".ffn_down.{i}.weight"
                    targets = [gatei, upi, downi]
                    tensors = self.load_multi(key, targets, device=device)
                    gate_it, up_it, down_it = tensors[gatei], tensors[upi], tensors[downi]
                    gate.append(gate_it)
                    up.append(up_it)
                    down.append(down_it)
                gate = torch.stack(gate)
                up = torch.stack(up)
                down = torch.stack(down)
                gate_type = self.gguf_loader.tensor_info[key + ".ffn_gate.0.weight"]["ggml_type"]
                up_type = self.gguf_loader.tensor_info[key + ".ffn_up.0.weight"]["ggml_type"]
                down_type = self.gguf_loader.tensor_info[key + ".ffn_down.0.weight"]["ggml_type"]
            else:
                raise ValueError(f"Experts {key} not found in gguf_loader")
            res = {key:{"gate": gate, "up": up, "down": down, "gate_type": gate_type, "up_type": up_type, "down_type": down_type}}
        return res
    
    def load_multi(self, key: str, keys: list[str], device: str = "cpu"):
        tensors = {}
        for k in keys:
            tensors[k] = self.gguf_loader.load_gguf_tensor(key + k, device=device)
        return tensors

    def dequantize_tensor(self, tensor, tensor_type, target_type="int8"):
        """Dequantize a tensor if it's in a quantized format like Q4_K
        
        Args:
            tensor: The tensor to dequantize
            tensor_type: The GGML quantization type
            target_type: Target format - "float32", "int8", or "bf16"
        """
        try:
            print(f"Attempting to dequantize tensor with type {tensor_type} to {target_type}")
            
            # For efficient conversion to int8, we'll implement direct paths
            if target_type == "int8" and tensor_type in [GGMLQuantizationType.Q4_K, GGMLQuantizationType.Q5_K, GGMLQuantizationType.Q6_K]:
                print(f"Using direct quantized->int8 conversion path for type {tensor_type}")
                
                # First get the tensor as float32 (temporarily)
                if tensor_type == GGMLQuantizationType.Q4_K:  # 12 = Q4_K
                    temp_float = dequantize_q4_k(tensor)
                elif tensor_type == GGMLQuantizationType.Q5_K:  # 13 = Q5_K
                    temp_float = dequantize_q5_k(tensor)
                elif tensor_type == GGMLQuantizationType.Q6_K:  # 14 = Q6_K
                    temp_float = dequantize_q6_k(tensor)
                
                # Convert to int8 directly (with proper scaling to utilize the int8 range)
                # Find the abs max to scale properly
                abs_max = np.max(np.abs(temp_float))
                if abs_max > 0:
                    scale = 127.0 / abs_max
                    # Convert directly to int8, clearing the float32 data as we go
                    tensor_int8 = np.empty(temp_float.shape, dtype=np.int8)
                    chunk_size = 1000000  # Process in chunks to reduce peak memory
                    for i in range(0, temp_float.size, chunk_size):
                        end = min(i + chunk_size, temp_float.size)
                        # Slice, scale, convert, assign
                        chunk = temp_float.flat[i:end]
                        tensor_int8.flat[i:end] = np.clip(np.round(chunk * scale), -127, 127).astype(np.int8)
                        # Clear the chunk from memory
                        del chunk
                
                    # Clear the float data to free memory
                    del temp_float
                    
                    print(f"Successfully converted to int8, shape: {tensor_int8.shape}")
                    return tensor_int8
                else:
                    # If all zeros, just create zeros
                    return np.zeros(temp_float.shape, dtype=np.int8)
            
            # Original path for float32 conversion
            if tensor_type == GGMLQuantizationType.Q4_K:  # 12 = Q4_K
                print("Using dequantize_q4_k to convert")
                # Convert quantized tensor to float32
                tensor_float32 = dequantize_q4_k(tensor)
            elif tensor_type == GGMLQuantizationType.Q5_K:  # 13 = Q5_K
                print("Using dequantize_q5_k to convert")
                tensor_float32 = dequantize_q5_k(tensor)
            elif tensor_type == GGMLQuantizationType.Q6_K:  # 14 = Q6_K
                print("Using dequantize_q6_k to convert")
                tensor_float32 = dequantize_q6_k(tensor)
            else:
                print(f"No dequantization function available for type {tensor_type}")
                return tensor
            
            # Convert to the requested format if needed
            if target_type == "int8":
                # Scale to int8 range (-127 to 127)
                abs_max = np.max(np.abs(tensor_float32))
                if abs_max > 0:
                    scale = 127.0 / abs_max
                    tensor_int8 = np.clip(np.round(tensor_float32 * scale), -127, 127).astype(np.int8)
                    return tensor_int8
                else:
                    return np.zeros(tensor_float32.shape, dtype=np.int8)
            elif target_type == "bf16":
                # Convert to bf16 using PyTorch
                tensor_torch = torch.tensor(tensor_float32)
                tensor_bf16 = tensor_torch.to(torch.bfloat16).numpy()
                return tensor_bf16
            else:
                # Return as float32
                return tensor_float32
                
        except Exception as e:
            print(f"Error during dequantization: {str(e)}")
            if self.force_amx:
                print("Continuing despite dequantization error (force_amx=True)")
            return tensor

class KExpertsCPU(KExpertsBase):
    input_tensor_cpu:Tensor = None
    expert_ids_cpu:Tensor = None
    weights_cpu:Tensor = None
    output_cpu:Tensor = None
    output_gpu_map:dict = {} # Manage output tensor buffer on different gpu
    #stream_map:dict = {} # Manage cuda stream on different gpu
    CPU_INFER = CPUInfer(Config().cpu_infer)
    def __init__(
        self,
        key: str,
        gguf_loader: GGUFLoader,
        config: PretrainedConfig,
        n_routed_experts: int,
        orig_module: nn.Module = None,
        device: str = "cpu",
        out_device: str = "cpu", # this device mean which device the output should on. TODO: support cpu.
        force_amx: bool = True,  # Force AMX to be used without fallback
        **kwargs
    ):
        super().__init__(key, gguf_loader, config, orig_module, device, **kwargs)
        assert device.lower() == "cpu", "KExpertsCPU can only be loaded on CPU"
        self.n_routed_experts = n_routed_experts
        self.out_device = out_device
        self.backend = kwargs.get("backend", "llamafile")
        self.max_chunk_size = Config().max_chunk_size
        self.force_amx = force_amx

    def convert_to_bf16(self, tensor, name, ptr_ref):
        """Safely convert a tensor to BF16 format with detailed error reporting"""
        try:
            print(f"Converting {name} tensor from {type(tensor)} to BF16 format")
            
            # Check if tensor might already be in BF16 format
            if hasattr(tensor, 'dtype') and str(tensor.dtype).lower() in ['bfloat16', 'bf16']:
                print(f"{name} appears to already be in BF16 format, skipping conversion")
                return tensor, ptr_ref
            
            # Try several approaches in sequence - the most likely to work first
            
            # Approach 1: Direct memory copy with PyTorch
            try:
                print(f"Trying PyTorch CPU tensor approach for {name}")
                # First create a float32 tensor from numpy
                tensor_float32 = torch.tensor(np.array(tensor, dtype=np.float32, copy=True), device="cpu")
                # Convert to bfloat16
                tensor_bf16_torch = tensor_float32.to(torch.bfloat16)
                # Create a numpy array with the same size as the bfloat16 tensor
                # but view it as uint16 (same size as bfloat16)
                buffer_size = tensor_bf16_torch.element_size() * tensor_bf16_torch.nelement()
                tensor_bf16_np = np.empty(tensor_bf16_torch.shape, dtype=np.uint16)
                
                # Copy the raw tensor data to the numpy array
                # Get PyTorch tensor pointer
                tensor_ptr = tensor_bf16_torch.data_ptr()
                # Get numpy array pointer
                np_ptr = tensor_bf16_np.ctypes.data
                
                # Use ctypes to copy memory
                ctypes.memmove(np_ptr, tensor_ptr, buffer_size)
                
                print(f"Successfully created BF16 data using PyTorch+memmove for {name}")
                tensor_bf16 = tensor_bf16_np
            except Exception as e:
                print(f"PyTorch approach failed: {str(e)}, trying fallback methods")
                
                # Approach 2: Check if NumPy supports BF16 directly (some newer versions do)
                try:
                    # Try direct numpy conversion if available
                    tensor_float32 = np.array(tensor, dtype=np.float32, copy=True)
                    tensor_bf16 = tensor_float32.astype(np.bfloat16)
                    print(f"Using numpy's native BF16 support for {name}")
                except (AttributeError, TypeError) as e2:
                    print(f"Numpy BF16 support not available: {str(e2)}")
                    
                    # Approach 3: Manual bit manipulation
                    print(f"Using manual bit manipulation for BF16 conversion of {name}")
                    # Convert float32 to uint16 using custom BF16 bit manipulation:
                    # 1. View the float32 array as uint32
                    # 2. Shift right by 16 bits to get the high bits
                    # 3. Cast to uint16
                    # This effectively truncates the mantissa, which is what BF16 does
                    tensor_float32 = np.array(tensor, dtype=np.float32, copy=True)
                    tensor_uint32 = tensor_float32.view(np.uint32)
                    tensor_bf16 = (tensor_uint32 >> 16).astype(np.uint16)
                
            # Get the new pointer
            new_ptr = ctypes.addressof(
                ctypes.cast(tensor_bf16.ctypes.data, ctypes.POINTER(ctypes.c_uint64)).contents
            )
            print(f"Successfully converted {name} to BF16, shape: {tensor_bf16.shape}")
            return tensor_bf16, new_ptr
            
        except Exception as e:
            print(f"ERROR converting {name} to BF16: {str(e)}")
            if self.force_amx:
                print(f"Detailed tensor info - Type: {type(tensor)}, Shape: {tensor.shape if hasattr(tensor, 'shape') else 'unknown'}")
                print(f"Memory layout - Size: {tensor.size if hasattr(tensor, 'size') else 'unknown'}, "
                      f"Itemsize: {tensor.itemsize if hasattr(tensor, 'itemsize') else 'unknown'}")
                raise RuntimeError(f"Failed to convert {name} to BF16 for AMX backend: {str(e)}")
            return tensor, ptr_ref

    def load(self, w: dict | nn.Parameter | tuple | None = None, device:str|None = None):
        if device:
            assert device.lower() == "cpu", "KExpertsCPU can only be loaded on CPU, Parameter \"device\" can be cpu or None."
        if w is None: w = self.load_weights()[self.key]
        self.gate = w["gate"]
        self.up = w["up"]
        self.down = w["down"]
        self.gate_type = w["gate_type"]
        self.up_type = w["up_type"]
        self.down_type = w["down_type"]
        
        # Print detailed debug info about weight types
        print(f"Original weights info:")
        print(f"- Gate: type={self.gate_type}, dtype={self.gate.dtype if hasattr(self.gate, 'dtype') else 'unknown'}, shape={self.gate.shape if hasattr(self.gate, 'shape') else 'unknown'}")
        print(f"- Up: type={self.up_type}, dtype={self.up.dtype if hasattr(self.up, 'dtype') else 'unknown'}, shape={self.up.shape if hasattr(self.up, 'shape') else 'unknown'}")
        print(f"- Down: type={self.down_type}, dtype={self.down.dtype if hasattr(self.down, 'dtype') else 'unknown'}, shape={self.down.shape if hasattr(self.down, 'shape') else 'unknown'}")
        
        # Special handling for quantized types like Q4_K (type 12)
        if self.backend in ["AMXBF16", "AMXInt8"]:
            # Try to dequantize if needed
            if self.gate_type == 12 or self.up_type == 12 or self.down_type == 12:  # Q4_K
                # For quantized weights, try using a different backend directly instead of conversion
                print(f"Detected quantized weights (gate={self.gate_type}, up={self.up_type}, down={self.down_type})")
                
                # Only switch backends if not forcing AMX
                if not self.force_amx:
                    # Let the llamafile backend handle this by setting flag to false
                    if not hasattr(self, '_warned_about_quantized'):
                        print("WARNING: AMX backends don't work well with quantized weights. Using 'llamafile' backend instead.")
                        print("To force AMX with BF16 conversion, set force_amx=True")
                        self._warned_about_quantized = True
                    # Use llamafile backend for quantized weights
                    self.backend = "llamafile"
                else:
                    print("FORCING AMX backend with quantized weights as requested (force_amx=True)")
                    print("This may fail or produce incorrect results, but will attempt conversion anyway.")
                    
                    # Try to dequantize if force_amx is True
                    print("Attempting to dequantize tensors before conversion...")
                    
                    # Choose target format based on backend
                    target_format = "bf16" if self.backend == "AMXBF16" else "int8"
                    print(f"Target format: {target_format} for backend {self.backend}")
                    
                    if self.gate_type in [12, 13, 14]:  # Q4_K, Q5_K, Q6_K
                        self.gate = self.dequantize_tensor(self.gate, self.gate_type, target_format)
                        # Set appropriate type
                        self.gate_type = GGMLQuantizationType.BF16 if target_format == "bf16" else GGMLQuantizationType.I8
                    
                    if self.up_type in [12, 13, 14]:
                        self.up = self.dequantize_tensor(self.up, self.up_type, target_format)
                        self.up_type = GGMLQuantizationType.BF16 if target_format == "bf16" else GGMLQuantizationType.I8
                    
                    if self.down_type in [12, 13, 14]:
                        self.down = self.dequantize_tensor(self.down, self.down_type, target_format)
                        self.down_type = GGMLQuantizationType.BF16 if target_format == "bf16" else GGMLQuantizationType.I8
                    
                    print(f"After dequantization:")
                    print(f"- Gate: type={self.gate_type}, dtype={self.gate.dtype if hasattr(self.gate, 'dtype') else 'unknown'}")
                    print(f"- Up: type={self.up_type}, dtype={self.up.dtype if hasattr(self.up, 'dtype') else 'unknown'}")
                    print(f"- Down: type={self.down_type}, dtype={self.down.dtype if hasattr(self.down, 'dtype') else 'unknown'}")
                  
        # Dump memory layout information for debugging
        print("\nMemory layout information:")
        for tensor_name, tensor in [("gate", self.gate), ("up", self.up), ("down", self.down)]:
            try:
                if hasattr(tensor, 'dtype'):
                    print(f"{tensor_name}: dtype={tensor.dtype}, shape={tensor.shape}, size={tensor.size if hasattr(tensor, 'size') else 'N/A'}")
                else:
                    print(f"{tensor_name}: (No dtype attribute) type={type(tensor)}")
                
                # Try to get ctypes pointer info
                try:
                    ptr = ctypes.addressof(ctypes.cast(tensor.ctypes.data, ctypes.POINTER(ctypes.c_uint64)).contents)
                    print(f"  - pointer address: {ptr:#x}")
                except (AttributeError, ValueError) as e:
                    print(f"  - pointer info not available: {str(e)}")
            except Exception as e:
                print(f"Error getting memory info for {tensor_name}: {str(e)}")
        
        gate_ptr = ctypes.addressof(
            ctypes.cast(self.gate.ctypes.data, ctypes.POINTER(ctypes.c_uint64)).contents
        )
        up_ptr = ctypes.addressof(
            ctypes.cast(self.up.ctypes.data, ctypes.POINTER(ctypes.c_uint64)).contents
        )
        down_ptr = ctypes.addressof(
            ctypes.cast(self.down.ctypes.data, ctypes.POINTER(ctypes.c_uint64)).contents
        )
        # print(self.gate_qtype, self.up_qtype, self.down_qtype)
        n_routed_experts = self.n_routed_experts
        # n_routed_experts = len(self.orig_module)
        
        self.cpu_infer = KExpertsCPU.CPU_INFER
        
        # Handle the different backends
        if self.backend == "llamafile":
            print("Using llamafile backend with standard MOEConfig")
            moe_config = MOEConfig(
                n_routed_experts,
                self.config.num_experts_per_tok,
                self.config.hidden_size,
                self.config.moe_intermediate_size,
                64,
                10,
                1024,
                gate_ptr,
                up_ptr,
                down_ptr,
                self.gate_type,
                self.up_type,
                self.down_type,
                30, # TODO: get from model.dtype
            )
            self.moe = MOE(moe_config)
            
        elif self.backend == "AMXBF16":
            # Try AMX backend but fall back to llamafile if there's an issue
            try:
                print(f"Initializing AMXBF16 backend (bfloat16)...")
                print(f"Tensor types - gate: {self.gate_type}, up: {self.up_type}, down: {self.down_type}")
                
                # AMX requires BF16 format - ensure all tensors are BF16
                if self.gate_type != GGMLQuantizationType.BF16:
                    self.gate, gate_ptr = self.convert_to_bf16(self.gate, "gate", gate_ptr)
                    self.gate_type = GGMLQuantizationType.BF16
                
                if self.up_type != GGMLQuantizationType.BF16:
                    self.up, up_ptr = self.convert_to_bf16(self.up, "up", up_ptr)
                    self.up_type = GGMLQuantizationType.BF16
                    
                if self.down_type != GGMLQuantizationType.BF16:
                    self.down, down_ptr = self.convert_to_bf16(self.down, "down", down_ptr)
                    self.down_type = GGMLQuantizationType.BF16
                
                print(f"Creating AMX_MOEConfig for AMXBF16 mode: experts={n_routed_experts}, experts_per_tok={self.config.num_experts_per_tok}, hidden={self.config.hidden_size}, intermediate={self.config.moe_intermediate_size}")
                    
                moe_config = AMX_MOEConfig(
                    n_routed_experts,
                    self.config.num_experts_per_tok,
                    self.config.hidden_size,
                    self.config.moe_intermediate_size,
                    25600,
                    gate_ptr,
                    up_ptr,
                    down_ptr,
                )
                
                print("Creating AMXBF16_MOE instance...")
                self.moe = AMXBF16_MOE(moe_config)
                
                print("Loading weights into AMXBF16 backend...")
                self.cpu_infer.submit(self.moe.load_weights())
                self.cpu_infer.sync()
                print(f"Successfully initialized AMXBF16 backend")
                
            except Exception as e:
                print(f"ERROR initializing AMXBF16: {str(e)}")
                
                if self.force_amx:
                    # If force_amx is True, don't fall back - raise the error
                    raise RuntimeError(f"Failed to initialize AMXBF16 and force_amx=True: {str(e)}")
                
                print(f"Falling back to llamafile backend")
                
                moe_config = MOEConfig(
                    n_routed_experts,
                    self.config.num_experts_per_tok,
                    self.config.hidden_size,
                    self.config.moe_intermediate_size,
                    64,
                    10,
                    1024,
                    gate_ptr,
                    up_ptr,
                    down_ptr,
                    self.gate_type,
                    self.up_type,
                    self.down_type,
                    30,  # TODO: get from model.dtype
                )
                self.moe = MOE(moe_config)
                
        elif self.backend == "AMXInt8":
            # Try AMX backend but fall back to llamafile if there's an issue
            try:
                print(f"Initializing AMXInt8 backend (int8)...")
                print(f"Tensor types - gate: {self.gate_type}, up: {self.up_type}, down: {self.down_type}")
                
                # For AMXInt8, we handle differently from AMXBF16
                # We either need to use BF16 tensors that AMXInt8_MOE will quantize internally,
                # or provide int8 data directly
                
                # If we have BF16 already, use it directly - AMXInt8 can handle this
                if self.gate_type != GGMLQuantizationType.BF16 and self.gate_type != GGMLQuantizationType.I8:
                    print("Converting gate to int8 for AMXInt8 backend")
                    self.gate, gate_ptr = self.dequantize_tensor(self.gate, self.gate_type, "int8")
                    self.gate_type = GGMLQuantizationType.I8
                
                if self.up_type != GGMLQuantizationType.BF16 and self.up_type != GGMLQuantizationType.I8:
                    print("Converting up to int8 for AMXInt8 backend")
                    self.up, up_ptr = self.dequantize_tensor(self.up, self.up_type, "int8")
                    self.up_type = GGMLQuantizationType.I8
                    
                if self.down_type != GGMLQuantizationType.BF16 and self.down_type != GGMLQuantizationType.I8:
                    print("Converting down to int8 for AMXInt8 backend")
                    self.down, down_ptr = self.dequantize_tensor(self.down, self.down_type, "int8")
                    self.down_type = GGMLQuantizationType.I8
                
                print(f"Creating AMX_MOEConfig for AMXInt8 mode: experts={n_routed_experts}, experts_per_tok={self.config.num_experts_per_tok}, hidden={self.config.hidden_size}, intermediate={self.config.moe_intermediate_size}")
                    
                moe_config = AMX_MOEConfig(
                    n_routed_experts,
                    self.config.num_experts_per_tok,
                    self.config.hidden_size,
                    self.config.moe_intermediate_size,
                    25600,
                    gate_ptr,
                    up_ptr,
                    down_ptr,
                )
                
                print("Creating AMXInt8_MOE instance...")
                self.moe = AMXInt8_MOE(moe_config)
                
                print("Loading weights into AMXInt8 backend...")
                self.cpu_infer.submit(self.moe.load_weights())
                self.cpu_infer.sync()
                print(f"Successfully initialized AMXInt8 backend")
                
            except Exception as e:
                print(f"ERROR initializing AMXInt8: {str(e)}")
                
                if self.force_amx:
                    # If force_amx is True, don't fall back - raise the error
                    raise RuntimeError(f"Failed to initialize AMXInt8 and force_amx=True: {str(e)}")
                
                print(f"Falling back to llamafile backend")
                
                moe_config = MOEConfig(
                    n_routed_experts,
                    self.config.num_experts_per_tok,
                    self.config.hidden_size,
                    self.config.moe_intermediate_size,
                    64,
                    10,
                    1024,
                    gate_ptr,
                    up_ptr,
                    down_ptr,
                    self.gate_type,
                    self.up_type,
                    self.down_type,
                    30,  # TODO: get from model.dtype
                )
                self.moe = MOE(moe_config)
        # print(n_routed_experts, hidden_size, moe_intermediate_size)
        num_experts_per_tok = self.config.num_experts_per_tok
        if self.out_device not in KExpertsCPU.output_gpu_map:
            KExpertsCPU.output_gpu_map[self.out_device] = torch.zeros((self.max_chunk_size, self.config.hidden_size), device=self.out_device)
        if KExpertsCPU.input_tensor_cpu == None:
            # Check if CUDA is available before using pin_memory
            use_pin_memory = torch.cuda.is_available()
            KExpertsCPU.input_tensor_cpu = torch.zeros((self.max_chunk_size, self.config.hidden_size), device="cpu", pin_memory=use_pin_memory)
            KExpertsCPU.expert_ids_cpu = torch.zeros((self.max_chunk_size, num_experts_per_tok), device="cpu", dtype=torch.long, pin_memory=use_pin_memory)
            KExpertsCPU.weights_cpu = torch.zeros((self.max_chunk_size, num_experts_per_tok), device="cpu", dtype=torch.float32, pin_memory=use_pin_memory)
            KExpertsCPU.output_cpu = torch.zeros((self.max_chunk_size, self.config.hidden_size), device="cpu", pin_memory=use_pin_memory, dtype=torch.bfloat16)
    
    def warmup(self):
        self.cpu_infer.submit(self.moe.warm_up())
        self.cpu_infer.sync()
            
    def copy_and_submit(self, input_tensor, expert_ids, weights):
        self.num_tokens = input_tensor.size(0)
        # Check if CUDA is available before using non_blocking=True
        use_non_blocking = torch.cuda.is_available()
        KExpertsCPU.input_tensor_cpu[:self.num_tokens].copy_(input_tensor, non_blocking=use_non_blocking)
        KExpertsCPU.expert_ids_cpu[:self.num_tokens].copy_(expert_ids, non_blocking=use_non_blocking)
        KExpertsCPU.weights_cpu[:self.num_tokens].copy_(weights, non_blocking=use_non_blocking)
        self.cpu_infer.submit_with_cuda_stream(torch.cuda.current_stream(self.out_device).cuda_stream if torch.cuda.is_available() else None, self.moe.forward(self.num_tokens, expert_ids.size(1), KExpertsCPU.expert_ids_cpu.data_ptr(), KExpertsCPU.weights_cpu.data_ptr(), KExpertsCPU.input_tensor_cpu.data_ptr(), KExpertsCPU.output_cpu.data_ptr()))
        
    def sync_and_copy(self):
        # Check if CUDA is available before using cuda_stream
        if torch.cuda.is_available():
            self.cpu_infer.sync_with_cuda_stream(torch.cuda.current_stream(self.out_device).cuda_stream)
            KExpertsCPU.output_gpu_map[self.out_device][:self.num_tokens].copy_(KExpertsCPU.output_cpu[:self.num_tokens], non_blocking=True)
        else:
            self.cpu_infer.sync()
            KExpertsCPU.output_gpu_map[self.out_device][:self.num_tokens].copy_(KExpertsCPU.output_cpu[:self.num_tokens])
        return KExpertsCPU.output_gpu_map[self.out_device][:self.num_tokens]

    def forward(self, input_tensor, expert_ids, weights):
        # generate, capture and run cuda graph
        # print(expert_ids)
        if input_tensor.size(0)==1 and torch.cuda.is_available() and torch.cuda.is_current_stream_capturing():
            # TODO: this branch is unreachable, but the shape of input_tensor([1,hidden_size]) and input_tensor_cpu([hidden_size]) is not compatible
            #print("capturing experts")
            use_non_blocking = torch.cuda.is_available()
            KExpertsCPU.input_tensor_cpu.copy_(input_tensor, non_blocking=use_non_blocking)
            KExpertsCPU.expert_ids_cpu.copy_(expert_ids, non_blocking=use_non_blocking)
            KExpertsCPU.weights_cpu.copy_(weights, non_blocking=use_non_blocking)
            
            if torch.cuda.is_available():
                self.cpu_infer.submit_with_cuda_stream(torch.cuda.current_stream().cuda_stream, self.moe.forward(1, expert_ids.size(1), KExpertsCPU.expert_ids_cpu.data_ptr(), KExpertsCPU.weights_cpu.data_ptr(), KExpertsCPU.input_tensor_cpu.data_ptr(), KExpertsCPU.output_cpu.data_ptr()))
                self.cpu_infer.sync_with_cuda_stream(torch.cuda.current_stream().cuda_stream)
                KExpertsCPU.output_gpu_map[self.out_device].copy_(KExpertsCPU.output_cpu, non_blocking=True)
            else:
                self.cpu_infer.submit(self.moe.forward(1, expert_ids.size(1), KExpertsCPU.expert_ids_cpu.data_ptr(), KExpertsCPU.weights_cpu.data_ptr(), KExpertsCPU.input_tensor_cpu.data_ptr(), KExpertsCPU.output_cpu.data_ptr()))
                self.cpu_infer.sync()
                KExpertsCPU.output_gpu_map[self.out_device].copy_(KExpertsCPU.output_cpu)
                
            return KExpertsCPU.output_gpu_map[self.out_device]
        else:
            input_tensor = input_tensor.contiguous().cpu()
            expert_ids = expert_ids.contiguous().cpu()
            weights = weights.contiguous().to(torch.float32).cpu()
            output = torch.empty_like(input_tensor).contiguous()
            self.cpu_infer.submit(self.moe.forward(expert_ids.size(0), expert_ids.size(1), expert_ids.data_ptr(), weights.data_ptr(), input_tensor.data_ptr(), output.data_ptr()))
            self.cpu_infer.sync()
            return output.to(device=object.__getattribute__(self, "out_device"))
    
    def unload(self):
        return

    def load_weights(self, override_key: str | None = None, device: str = "cpu"):
        res = {}
        if override_key is not None:
            keys = override_key
        else:
            keys = [self.key]

        gate = None
        up = None
        down = None
        gate_type = None
        up_type = None
        down_type = None

        for key in keys:
            if key + ".ffn_gate_exps.weight" in self.gguf_loader.tensor_info:
                gate = self.gguf_loader.get_mmap_tensor(key + ".ffn_gate_exps.weight")
                up = self.gguf_loader.get_mmap_tensor(key + ".ffn_up_exps.weight")
                down = self.gguf_loader.get_mmap_tensor(key + ".ffn_down_exps.weight")
                gate_type = self.gguf_loader.tensor_info[key + ".ffn_gate_exps.weight"]["ggml_type"]
                up_type = self.gguf_loader.tensor_info[key + ".ffn_up_exps.weight"]["ggml_type"]
                down_type = self.gguf_loader.tensor_info[key + ".ffn_down_exps.weight"]["ggml_type"]
            elif key + ".ffn_down.0.weight" in self.gguf_loader.tensor_info:
                # for supporting  Mixtral-8x7B-Instuct  
                gate = []
                up = []
                down = []
                for i in range(8):
                    gate_it = self.gguf_loader.get_mmap_tensor(f"{key}.ffn_gate.{i}.weight")
                    up_it = self.gguf_loader.get_mmap_tensor(f"{key}.ffn_up.{i}.weight")
                    down_it = self.gguf_loader.get_mmap_tensor(f"{key}.ffn_down.{i}.weight")
                    gate.append(gate_it)
                    up.append(up_it)
                    down.append(down_it)
                gate = np.stack(gate)
                up = np.stack(up)
                down = np.stack(down)
                gate_type = self.gguf_loader.tensor_info[key + ".ffn_gate.0.weight"]["ggml_type"]
                up_type = self.gguf_loader.tensor_info[key + ".ffn_up.0.weight"]["ggml_type"]
                down_type = self.gguf_loader.tensor_info[key + ".ffn_down.0.weight"]["ggml_type"]
            else:
                raise ValueError(f"Experts {key} not found in gguf_loader")
            res = {key:{"gate": gate, "up": up, "down": down, "gate_type": gate_type, "up_type": up_type, "down_type": down_type}}
        return res
    
class KExpertsMarlin(KExpertsBase):
    expert_num: int
    loaded_experts_idx: list[int]
    def __init__(
        self,
        key: str,
        gguf_loader: GGUFLoader,
        config: PretrainedConfig,
        n_routed_experts: int,
        orig_module: nn.Module = None,
        device: str = "cpu",
        **kwargs
    ):
        super().__init__(key, gguf_loader, config, orig_module, device, **kwargs)
        self.expert_num = n_routed_experts
        self.loaded_experts_idx = []
        self.act_fn = ACT2FN[config.hidden_act]
        assert device.lower() != "cpu", "Marlin experts can only be loaded on GPU"
        self.device = device
        # create empty marlin experts according to the number of experts per token
        # up
        self.up_projs = [KLinearMarlin(key+ "." + "ffn_up_exps", gguf_loader, config, device=device) for i in range(self.expert_num)]
        # gate
        self.gate_projs = [KLinearMarlin(key+ "." + "ffn_gate_exps", gguf_loader, config, device=device) for i in range(self.expert_num)]
        # down
        self.down_projs = [KLinearMarlin(key+ "." + "ffn_down_exps", gguf_loader, config, device=device) for i in range(self.expert_num)]

    def load(self, w: dict | nn.Parameter | tuple | None = None, device: str | None = None):
        if device is None: device = self.device
        assert device.lower() != "cpu", "Marlin experts can only be loaded on GPU"
        if w is None: w = self.load_weights()[self.key]

        if isinstance(w, dict):
            self.gate = nn.Parameter(torch.from_numpy(w["gate"]))
            self.up = nn.Parameter(torch.from_numpy(w["up"]))
            self.down = nn.Parameter(torch.from_numpy(w["down"]))
            for i in range(self.expert_num):
                self.up_projs[i].load(self.up[i,...], device=device)
                self.gate_projs[i].load(self.gate[i,...], device=device)
                self.down_projs[i].load(self.down[i,...], device=device)
                self.loaded_experts_idx.append(i)
        return 

    def unload(self):
        for i in self.loaded_experts_idx:
            self.up_projs[i].unload()
            self.gate_projs[i].unload()
            self.down_projs[i].unload()
        self.loaded_experts_idx = []

    def load_weights(self, override_key: str | None = None):
        res = {}
        if override_key is not None:
            keys = override_key
        else:
            keys = [self.key]

        gate = None
        up = None
        down = None
        gate_type = None
        up_type = None
        down_type = None

        for key in keys:
            if key + ".ffn_gate_exps.weight" in self.gguf_loader.tensor_info:
                gate = self.gguf_loader.load_gguf_tensor(key + ".ffn_gate_exps.weight")
                up = self.gguf_loader.load_gguf_tensor(key + ".ffn_up_exps.weight")
                down = self.gguf_loader.load_gguf_tensor(key + ".ffn_down_exps.weight")
                gate_type = self.gguf_loader.tensor_info[key + ".ffn_gate_exps.weight"]["ggml_type"]
                up_type = self.gguf_loader.tensor_info[key + ".ffn_up_exps.weight"]["ggml_type"]
                down_type = self.gguf_loader.tensor_info[key + ".ffn_down_exps.weight"]["ggml_type"]
            elif key + ".ffn_down.0.weight" in self.gguf_loader.tensor_info:
                # for supporting  Mixtral-8x7B-Instuct  
                gate = []
                up = []
                down = []
                for i in range(8):
                    gate_it = self.gguf_loader.get_mmap_tensor(f"{key}.ffn_gate.{i}.weight")
                    up_it = self.gguf_loader.get_mmap_tensor(f"{key}.ffn_up.{i}.weight")
                    down_it = self.gguf_loader.get_mmap_tensor(f"{key}.ffn_down.{i}.weight")
                    gate.append(gate_it)
                    up.append(up_it)
                    down.append(down_it)
                gate = np.stack(gate)
                up = np.stack(up)
                down = np.stack(down)
                gate_type = self.gguf_loader.tensor_info[key + ".ffn_gate.0.weight"]["ggml_type"]
                up_type = self.gguf_loader.tensor_info[key + ".ffn_up.0.weight"]["ggml_type"]
                down_type = self.gguf_loader.tensor_info[key + ".ffn_down.0.weight"]["ggml_type"]
            else:
                raise ValueError(f"Experts {key} not found in gguf_loader")
            res = {key:{"gate": gate, "up": up, "down": down, "gate_type": gate_type, "up_type": up_type, "down_type": down_type}}
        return res

    def forward(self, input_tensor:torch.Tensor, expert_ids, weights):
        # forward
        device = input_tensor.device
        input_tensor = input_tensor.to("cpu")
        outs = torch.zeros_like(input_tensor)
        for expert_idx in range(expert_ids.size(0)):
            down_proj = self.down_projs[expert_idx]
            gate_proj = self.gate_projs[expert_idx]
            up_proj = self.up_projs[expert_idx]

            outs += down_proj(self.act_fn(gate_proj(input_tensor)) * up_proj(input_tensor)) * weights[expert_idx]
        outs = outs.to(device)
        return outs

class KExpertsTorch(KExpertsBase):
    expert_num: int
    loaded_experts_idx: list[int]
    gate: torch.Tensor
    up: torch.Tensor
    down: torch.Tensor
    def __init__(
        self,
        key: str,
        gguf_loader: GGUFLoader,
        config: PretrainedConfig,
        n_routed_experts: int,
        orig_module: nn.Module = None,
        device: str = "cpu",
        **kwargs
    ):
        super().__init__(key, gguf_loader, config, orig_module, device, **kwargs)
        self.expert_num = n_routed_experts
        # self.loaded_experts_idx = []
        self.act_fn = ACT2FN[config.hidden_act]
        self.device = device
        self.gate = None
        self.up = None
        self.donw = None
        self.dtype = torch.get_default_dtype()

    def load(self, w: dict | nn.Parameter | tuple | None = None, device: str | None = None):
        if device is None: device = self.device
        if w is None: w = self.load_weights(device=device)[self.key]

        if isinstance(w, dict):
            self.gate = w["gate"].to(device=device, dtype=self.dtype)
            self.up = w["up"].to(device=device, dtype=self.dtype)
            self.down = w["down"].to(device=device, dtype=self.dtype)

    def unload(self):
        if self.gate is not None:
            self.gate = None
            self.up = None
            self.down = None

    def forward(self, hidden_states_cpu: torch.Tensor, selected_experts_cpu: torch.Tensor, routing_weights_cpu: torch.Tensor) -> torch.Tensor:

        org_device = hidden_states_cpu.device
        hidden_states_cpu = hidden_states_cpu.to(self.device)
        selected_experts_cpu = selected_experts_cpu.to(self.device)
        routing_weights_cpu = routing_weights_cpu.to(self.device)
        
        batch_sequence_length, hidden_dim = hidden_states_cpu.size()

        final_hidden_states = torch.zeros(
            (batch_sequence_length, hidden_dim), dtype=self.gate.dtype, device=hidden_states_cpu.device
        )
        org_dtype = hidden_states_cpu.dtype
        hidden_states_cpu = hidden_states_cpu.to(self.gate.dtype)
        routing_weights_cpu = routing_weights_cpu.to(self.gate.dtype)
        # One hot encode the selected experts to create an expert mask
        # this will be used to easily index which expert is going to be sollicitated
        expert_mask = torch.nn.functional.one_hot(selected_experts_cpu, num_classes=self.expert_num).permute(2, 1, 0)

        # Loop over all available experts in the model and perform the computation on each expert
        for expert_idx in range(self.expert_num):
            idx, top_x = torch.where(expert_mask[expert_idx])
            # Index the correct hidden states and compute the expert hidden state for
            # the current expert. We need to make sure to multiply the output hidden
            # states by `routing_weights` on the corresponding tokens (top-1 and top-2)
            current_state = hidden_states_cpu[None, top_x].reshape(-1, hidden_dim)
            G = current_state @ self.gate[expert_idx,...].T
            A = self.act_fn(G)
            U = current_state @ self.up[expert_idx,...].T
            H = A * U  # Element-wise multiplication
            current_hidden_states = H @ self.down[expert_idx,...].T * routing_weights_cpu[top_x, idx, None]
            # However `index_add_` only support torch tensors for indexing so we'll use
            # the `top_x` tensor here.
            final_hidden_states.index_add_(0, top_x, current_hidden_states)


        return final_hidden_states.to(dtype=org_dtype, device=org_device)

EXPERTS_MAP = {
    "KExpertsCPU": KExpertsCPU,
    "KExpertsTorch": KExpertsTorch,
    "KExpertsMarlin": KExpertsMarlin,
}

class KTransformersExperts(BaseInjectedModule, KExpertsBase):
    def __init__(self,
                 key: str,
                 gguf_loader: GGUFLoader,
                 config: PretrainedConfig,
                 orig_module: nn.Module,
                #  device: str = "cpu",
                 prefill_device:str = "cpu",
                 prefill_op: str | None = "KExpertsTorch",
                 generate_device: str = "cpu",
                 generate_op: str | None = "KExpertsCPU",
                 **kwargs):
        BaseInjectedModule.__init__(self, key, gguf_loader, config, orig_module, generate_device, **kwargs)
        KExpertsBase.__init__(self, key, gguf_loader, config, orig_module, generate_device, **kwargs)
        if generate_op is not None:
            self.generate_experts = EXPERTS_MAP[generate_op](key, gguf_loader, config, len(orig_module), device=generate_device, **kwargs)
        else:
            self.generate_experts = None
        if prefill_op is not None:
            self.prefill_experts = EXPERTS_MAP[prefill_op](key, gguf_loader, config, len(orig_module), device=prefill_device, **kwargs)
        else:
            self.prefill_experts = None
        self.gpu_mlp_type = prefill_op
        self.cpu_mlp_type = generate_op
        self.mode = InferenceState.UNLOAD

    def load(self, w: dict = None,  mode: InferenceState = None):
        # TODO support w as input
        if not mode: mode = InferenceState.GENERATE
        if mode == InferenceState.GENERATE:
            self.prefill_experts.unload()
            self.generate_experts.load(w)
            self.device = self.generate_experts.device
            self.mode = mode
        elif mode == InferenceState.PREFILL:
            self.generate_experts.unload()
            self.prefill_experts.load(w)
            self.device = self.prefill_experts.device
            self.mode = mode
        elif mode == InferenceState.UNLOAD:
            self.unload()
            self.mode = mode
            self.device = self.generate_experts.device
        else:
            raise ValueError("mode must be either InferenceState.GENERATE, InferenceState.PREFILL or InferenceState.UNLOAD")
        
    def warmup(self,  mode: InferenceState = None):
        if not mode: mode = InferenceState.GENERATE
        if mode == InferenceState.GENERATE:
            self.generate_experts.warmup()
        elif mode == InferenceState.PREFILL:
            self.prefill_experts.warmup()
        else:
            raise ValueError("mode must be either InferenceState.GENERATE or InferenceState.PREFILL")


    def unload(self):
        if self.generate_experts is not None:
            self.generate_experts.unload()
        if self.prefill_experts is not None:
            self.prefill_experts.unload()
        self.device = self.generate_experts.device

    def forward(self, input_tensor, expert_ids, weights):
        if self.mode == InferenceState.GENERATE:
            assert self.generate_experts is not None, "generate_experts is None"
            return self.generate_experts.forward(input_tensor, expert_ids, weights)
        elif self.mode == InferenceState.PREFILL:
            assert self.prefill_experts is not None, "prefill_experts is None"
            return self.prefill_experts.forward(input_tensor, expert_ids, weights)
        else:
            raise ValueError("load or set_inference_mode before forward")

    def set_inference_mode(self, mode: InferenceState):
        if mode == InferenceState.GENERATE:
            self.load(mode=InferenceState.GENERATE)
        elif mode == InferenceState.PREFILL:
            self.load(mode=InferenceState.PREFILL)
        elif mode == InferenceState.UNLOAD:
            self.unload()
        else:
            raise ValueError("mode must be either InferenceState.GENERATE, InferenceState.PREFILL or InferenceState.UNLOAD")


from ktransformers.models.modeling_deepseek import DeepseekV2MoE
from ktransformers.models.modeling_deepseek_v3 import DeepseekV3MoE
from ktransformers.models.modeling_qwen2_moe import Qwen2MoeSparseMoeBlock
from ktransformers.models.modeling_mixtral import MixtralSparseMoeBlock


class KQwen2MoeSparseMoeBlock(BaseInjectedModule, Qwen2MoeSparseMoeBlock):
    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        """ """
        orig_shape = hidden_states.shape
        batch_size, sequence_length, hidden_dim = hidden_states.shape
        hidden_states = hidden_states.view(-1, hidden_dim)
        # router_logits: (batch * sequence_length, n_experts)
        router_logits = self.gate(hidden_states)

        routing_weights = F.softmax(router_logits, dim=1, dtype=torch.float)
        routing_weights, selected_experts = torch.topk(routing_weights, self.top_k, dim=-1)
        if self.norm_topk_prob:
            routing_weights /= routing_weights.sum(dim=-1, keepdim=True)
        # we cast back to the input dtype
        routing_weights = routing_weights.to(hidden_states.dtype)
        
        # TODO: support prfill_experts when sequence_length != 1 and <= max_chunk_size
        if sequence_length <= Config().max_chunk_size and hasattr(self.experts.generate_experts, "copy_and_submit"):
            self.experts.generate_experts.copy_and_submit(hidden_states, selected_experts, routing_weights)
            shared_expert_output = self.shared_expert(hidden_states)
            shared_expert_output = F.sigmoid(self.shared_expert_gate(hidden_states)) * shared_expert_output
            y = self.experts.generate_experts.sync_and_copy()
            y += shared_expert_output
            y.resize_(*orig_shape)
            return y, router_logits
        
        # this branch is unreached
        if sequence_length <= Config().max_chunk_size and hasattr(self.experts.prefill_experts, "copy_and_submit"):
            self.experts.prefill_experts.copy_and_submit(hidden_states[0], selected_experts[0], routing_weights[0])
            shared_expert_output = self.shared_expert(hidden_states)
            shared_expert_output = F.sigmoid(self.shared_expert_gate(hidden_states)) * shared_expert_output
            y = self.experts.prefill_experts.sync_and_copy().unsqueeze(0)
            y += shared_expert_output
            y.resize_(*orig_shape)
            return y, router_logits
        
        hidden_states_expert = hidden_states.to(self.experts.device)  if isinstance(self.experts, KExpertsBase) else hidden_states_expert.cpu()
        selected_experts_expert = selected_experts.to(self.experts.device) if isinstance(self.experts, KExpertsBase) else selected_experts_expert.cpu()
        routing_weights_expert = routing_weights.to(self.experts.device) if isinstance(self.experts, KExpertsBase) else routing_weights_expert.cpu()

        shared_expert_output = self.shared_expert(hidden_states)
        shared_expert_output = (
            F.sigmoid(self.shared_expert_gate(hidden_states)) * shared_expert_output
        )

        if isinstance(self.experts, KExpertsBase):
            y = (
                self.moe_on_cpuinfer(
                    hidden_states_expert, selected_experts_expert, routing_weights_expert
                )
                .view(*orig_shape)
                .to(device=hidden_states.device)
            )
        elif hidden_states_expert.size(0) > 10:
            y = self.moe_infer(
                hidden_states_expert, selected_experts_expert, routing_weights_expert, orig_shape
            ).to(device=hidden_states.device)
        else:
            y = self.moe_infer_simple(
                hidden_states_expert, selected_experts_expert, routing_weights_expert
            ).to(device=hidden_states.device)
        y += shared_expert_output
        y.resize_(*orig_shape)
        return y, router_logits
    
    @torch.no_grad()
    def moe_on_cpuinfer(self, x: torch.Tensor, topk_ids: torch.Tensor, topk_weight: torch.Tensor) -> torch.Tensor:
        outs = torch.empty_like(x)
        outs = self.experts(x, topk_ids, topk_weight)
        return outs

    @torch.no_grad()
    # TODO may bugs here
    def moe_infer_simple(self, hidden_states_cpu: torch.Tensor, selected_experts_cpu: torch.Tensor, routing_weights_cpu: torch.Tensor) -> torch.Tensor:
        '''
        hidden_states_cpu: [num_tokens, hidden_size]
        topk_ids, topk_weight: [num_tokens, num_selected_experts]
        '''
        outs = torch.zeros_like(hidden_states_cpu)
        for token_idx in range(selected_experts_cpu.size(0)):
            for expert_idx in range(selected_experts_cpu.size(1)):
                expert = self.experts[selected_experts_cpu[token_idx, expert_idx]]
                outs[token_idx] += expert.forward(hidden_states_cpu[token_idx]) * routing_weights_cpu[token_idx, expert_idx]
        return outs
    
    @torch.no_grad()
    # TODO may bugs here
    def moe_infer(self, hidden_states_cpu: torch.Tensor, selected_experts_cpu: torch.Tensor, routing_weights_cpu: torch.Tensor, orig_shape: tuple) -> torch.Tensor:
        
        batch_size, sequence_length, hidden_dim = orig_shape

        final_hidden_states = torch.zeros(
            (batch_size * sequence_length, hidden_dim), dtype=hidden_states_cpu.dtype, device=hidden_states_cpu.device
        )

        # One hot encode the selected experts to create an expert mask
        # this will be used to easily index which expert is going to be sollicitated
        expert_mask = torch.nn.functional.one_hot(selected_experts_cpu, num_classes=self.num_experts).permute(2, 1, 0)

        # Loop over all available experts in the model and perform the computation on each expert
        for expert_idx in range(self.num_experts):
            expert_layer = self.experts[expert_idx]
            idx, top_x = torch.where(expert_mask[expert_idx])

            # Index the correct hidden states and compute the expert hidden state for
            # the current expert. We need to make sure to multiply the output hidden
            # states by `routing_weights` on the corresponding tokens (top-1 and top-2)
            current_state = hidden_states_cpu[None, top_x].reshape(-1, hidden_dim)
            current_hidden_states = expert_layer.forward(current_state) * routing_weights_cpu[top_x, idx, None]

            # However `index_add_` only support torch tensors for indexing so we'll use
            # the `top_x` tensor here.
            final_hidden_states.index_add_(0, top_x, current_hidden_states.to(hidden_states_cpu.dtype))

        return final_hidden_states

class KDeepseekV2MoE(BaseInjectedModule, DeepseekV2MoE):
    def forward(self, hidden_states):
        identity = hidden_states
        orig_shape = hidden_states.shape
        sequence_length = orig_shape[1]
        topk_idx, topk_weight, aux_loss = self.gate(hidden_states)
        hidden_states = hidden_states.view(-1, hidden_states.shape[-1])
        
        # TODO: support prfill_experts when sequence_length != 1 and <= max_chunk_size
        if sequence_length <= Config().max_chunk_size and hasattr(self.experts.generate_experts, "copy_and_submit"):
            self.experts.generate_experts.copy_and_submit(hidden_states, topk_idx, topk_weight)
            if self.config.n_shared_experts is not None:
                y_ = self.shared_experts(identity).squeeze(0)
            y = self.experts.generate_experts.sync_and_copy()
            y += y_
            y.resize_(*orig_shape)
            return y

        if self.config.n_shared_experts is not None:
            y_ = self.shared_experts(identity).squeeze(0)
            
        if isinstance(self.experts, KExpertsBase):
            y = self.moe_on_cpuinfer(hidden_states, topk_idx, topk_weight).view(*orig_shape).to(device=hidden_states.device)
        elif hidden_states.size(0) > 10:
            # TODO may bugs here
            y = (
                self.moe_infer(hidden_states, topk_idx, topk_weight)
                .view(*orig_shape)
                .to(device=hidden_states.device)
            )
        else:
            # TODO may bugs here
            y = (
                self.moe_infer_simple(hidden_states, topk_idx, topk_weight)
                .view(*orig_shape)
                .to(device=hidden_states.device)
            )
        if self.config.n_shared_experts is not None:
            y += y_
        return y

    @torch.no_grad()
    def moe_on_cpuinfer(self, x: torch.Tensor, topk_ids: torch.Tensor, topk_weight: torch.Tensor) -> torch.Tensor:
        outs = torch.empty_like(x)
        outs = self.experts(x, topk_ids, topk_weight)
        return outs

    @torch.no_grad()
    # TODO may bugs here
    def moe_infer_simple(
        self, x: torch.Tensor, topk_ids: torch.Tensor, topk_weight: torch.Tensor
    ) -> torch.Tensor:
        """
        x: [num_tokens, hidden_size]
        topk_ids, topk_weight: [num_tokens, num_selected_experts]
        """
        outs = torch.zeros_like(x)
        for token_idx in range(topk_ids.size(0)):
            for expert_idx in range(topk_ids.size(1)):
                expert = self.experts[topk_ids[token_idx, expert_idx]]
                outs[token_idx] += (
                    expert.forward(x[token_idx]) * topk_weight[token_idx, expert_idx]
                )
        return outs

    @torch.no_grad()
    # TODO may bugs here
    def moe_infer(self, x, topk_ids, topk_weight):
        cnts = topk_ids.new_zeros((topk_ids.shape[0], len(self.experts)))
        cnts.scatter_(1, topk_ids, 1)
        tokens_per_expert = cnts.sum(dim=0)
        idxs = topk_ids.view(-1).argsort()
        sorted_tokens = x[idxs // topk_ids.shape[1]]
        tokens_per_expert = tokens_per_expert.cpu().numpy()

        outputs = []
        start_idx = 0
        for i, num_tokens in enumerate(tokens_per_expert):
            end_idx = start_idx + num_tokens
            if num_tokens == 0:
                continue
            expert = self.experts[i + self.ep_rank * self.experts_per_rank]
            tokens_for_this_expert = sorted_tokens[start_idx:end_idx]
            expert_out = expert.forward(tokens_for_this_expert)
            outputs.append(expert_out)
            start_idx = end_idx

        outs = torch.cat(outputs, dim=0) if len(outputs) else sorted_tokens.new_empty(0)

        new_x = torch.empty_like(outs)
        new_x[idxs] = outs
        final_out = (
            new_x.view(*topk_ids.shape, -1)
            .type(topk_weight.dtype)
            .mul_(topk_weight.unsqueeze(dim=-1))
            .sum(dim=1)
            .type(new_x.dtype)
        )
        return final_out

class KDeepseekV3MoE(BaseInjectedModule, DeepseekV3MoE):
    
    def forward(self, hidden_states):
        identity = hidden_states
        orig_shape = hidden_states.shape
        sequence_length = orig_shape[1]
        topk_idx, topk_weight = self.gate(hidden_states)
        hidden_states = hidden_states.view(-1, hidden_states.shape[-1])
        
        # TODO: support prfill_experts when sequence_length != 1 and <= max_chunk_size
        if sequence_length <= Config().max_chunk_size and hasattr(self.experts.generate_experts, "copy_and_submit"):
            self.experts.generate_experts.copy_and_submit(hidden_states, topk_idx, topk_weight)
            if self.config.n_shared_experts is not None:
                y_ = self.shared_experts(identity).squeeze(0)
            y = self.experts.generate_experts.sync_and_copy()
            y += y_
            y.resize_(*orig_shape)
            return y

        if self.config.n_shared_experts is not None:
            y_ = self.shared_experts(identity).squeeze(0)
            
        if isinstance(self.experts, KExpertsBase):
            y = self.moe_on_cpuinfer(hidden_states, topk_idx, topk_weight).view(*orig_shape).to(device=hidden_states.device)
        elif hidden_states.size(0) > 10:
            # TODO may bugs here
            y = (
                self.moe_infer(hidden_states, topk_idx, topk_weight)
                .view(*orig_shape)
                .to(device=hidden_states.device)
            )
        else:
            # TODO may bugs here
            y = (
                self.moe_infer_simple(hidden_states, topk_idx, topk_weight)
                .view(*orig_shape)
                .to(device=hidden_states.device)
            )
        if self.config.n_shared_experts is not None:
            y += y_
        return y

    @torch.no_grad()
    def moe_on_cpuinfer(self, x: torch.Tensor, topk_ids: torch.Tensor, topk_weight: torch.Tensor) -> torch.Tensor:
        outs = torch.empty_like(x)
        outs = self.experts(x, topk_ids, topk_weight)
        return outs

    @torch.no_grad()
    # TODO may bugs here
    def moe_infer_simple(
        self, x: torch.Tensor, topk_ids: torch.Tensor, topk_weight: torch.Tensor
    ) -> torch.Tensor:
        """
        x: [num_tokens, hidden_size]
        topk_ids, topk_weight: [num_tokens, num_selected_experts]
        """
        outs = torch.zeros_like(x)
        for token_idx in range(topk_ids.size(0)):
            for expert_idx in range(topk_ids.size(1)):
                expert = self.experts[topk_ids[token_idx, expert_idx]]
                outs[token_idx] += (
                    expert.forward(x[token_idx]) * topk_weight[token_idx, expert_idx]
                )
        return outs

    @torch.no_grad()
    # TODO may bugs here
    def moe_infer(self, x, topk_ids, topk_weight):
        cnts = topk_ids.new_zeros((topk_ids.shape[0], len(self.experts)))
        cnts.scatter_(1, topk_ids, 1)
        tokens_per_expert = cnts.sum(dim=0)
        idxs = topk_ids.view(-1).argsort()
        sorted_tokens = x[idxs // topk_ids.shape[1]]
        tokens_per_expert = tokens_per_expert.cpu().numpy()

        outputs = []
        start_idx = 0
        for i, num_tokens in enumerate(tokens_per_expert):
            end_idx = start_idx + num_tokens
            if num_tokens == 0:
                continue
            expert = self.experts[i + self.ep_rank * self.experts_per_rank]
            tokens_for_this_expert = sorted_tokens[start_idx:end_idx]
            expert_out = expert.forward(tokens_for_this_expert)
            outputs.append(expert_out)
            start_idx = end_idx

        outs = torch.cat(outputs, dim=0) if len(outputs) else sorted_tokens.new_empty(0)

        new_x = torch.empty_like(outs)
        new_x[idxs] = outs
        final_out = (
            new_x.view(*topk_ids.shape, -1)
            .type(topk_weight.dtype)
            .mul_(topk_weight.unsqueeze(dim=-1))
            .sum(dim=1)
            .type(new_x.dtype)
        )
        return final_out

class KMistralSparseMoEBlock(BaseInjectedModule, MixtralSparseMoeBlock):
    
    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        """ """
        orig_shape = hidden_states.shape
        batch_size, sequence_length, hidden_dim = hidden_states.shape
        if self.training and self.jitter_noise > 0:
            hidden_states *= torch.empty_like(hidden_states).uniform_(1.0 - self.jitter_noise, 1.0 + self.jitter_noise)
        hidden_states = hidden_states.view(-1, hidden_dim)
        # router_logits: (batch * sequence_length, n_experts)
        router_logits = self.gate(hidden_states)

        routing_weights = F.softmax(router_logits, dim=1, dtype=torch.float)
        routing_weights, selected_experts = torch.topk(routing_weights, self.top_k, dim=-1)
        routing_weights /= routing_weights.sum(dim=-1, keepdim=True)
        # we cast back to the input dtype
        routing_weights = routing_weights.to(hidden_states.dtype)
        
        if sequence_length == 1 and hasattr(self.experts.generate_experts, "submit_for_one_decode"):
            self.experts.generate_experts.submit_for_one_decode(hidden_states[0], selected_experts[0], routing_weights[0])
            y = self.experts.generate_experts.sync_for_one_decode().unsqueeze(0)
            y.resize_(*orig_shape)
            return y, router_logits
        
        hidden_states_expert = hidden_states.to(self.experts.device)  if isinstance(self.experts, KExpertsBase) else hidden_states_expert.cpu()
        selected_experts_expert = selected_experts.to(self.experts.device) if isinstance(self.experts, KExpertsBase) else selected_experts_expert.cpu()
        routing_weights_expert = routing_weights.to(self.experts.device) if isinstance(self.experts, KExpertsBase) else routing_weights_expert.cpu()

        if isinstance(self.experts, KExpertsBase):
            y = (
                self.moe_on_cpuinfer(
                    hidden_states_expert, selected_experts_expert, routing_weights_expert
                )
                .view(*orig_shape)
                .to(device=hidden_states.device)
            )
        elif hidden_states_expert.size(0) > 10:
            y = self.moe_infer(
                hidden_states_expert, selected_experts_expert, routing_weights_expert, orig_shape
            ).to(device=hidden_states.device)
        else:
            y = self.moe_infer_simple(
                hidden_states_expert, selected_experts_expert, routing_weights_expert
            ).to(device=hidden_states.device)
            
        y.resize_(*orig_shape)
        return y, router_logits
    
    @torch.no_grad()
    def moe_on_cpuinfer(self, x: torch.Tensor, topk_ids: torch.Tensor, topk_weight: torch.Tensor) -> torch.Tensor:
        outs = torch.empty_like(x)
        outs = self.experts(x, topk_ids, topk_weight)
        return outs

    @torch.no_grad()
    # TODO may bugs here
    def moe_infer_simple(self, hidden_states_cpu: torch.Tensor, selected_experts_cpu: torch.Tensor, routing_weights_cpu: torch.Tensor) -> torch.Tensor:
        '''
        hidden_states_cpu: [num_tokens, hidden_size]
        topk_ids, topk_weight: [num_tokens, num_selected_experts]
        '''
        outs = torch.zeros_like(hidden_states_cpu)
        for token_idx in range(selected_experts_cpu.size(0)):
            for expert_idx in range(selected_experts_cpu.size(1)):
                expert = self.experts[selected_experts_cpu[token_idx, expert_idx]]
                outs[token_idx] += expert.forward(hidden_states_cpu[token_idx]) * routing_weights_cpu[token_idx, expert_idx]
        return outs
    
    @torch.no_grad()
    # TODO may bugs here
    def moe_infer(self, hidden_states_cpu: torch.Tensor, selected_experts_cpu: torch.Tensor, routing_weights_cpu: torch.Tensor, orig_shape: tuple) -> torch.Tensor:
        
        batch_size, sequence_length, hidden_dim = orig_shape

        final_hidden_states = torch.zeros(
            (batch_size * sequence_length, hidden_dim), dtype=hidden_states_cpu.dtype, device=hidden_states_cpu.device
        )

        # One hot encode the selected experts to create an expert mask
        # this will be used to easily index which expert is going to be sollicitated
        expert_mask = torch.nn.functional.one_hot(selected_experts_cpu, num_classes=self.num_experts).permute(2, 1, 0)

        # Loop over all available experts in the model and perform the computation on each expert
        for expert_idx in range(self.num_experts):
            expert_layer = self.experts[expert_idx]
            idx, top_x = torch.where(expert_mask[expert_idx])

            # Index the correct hidden states and compute the expert hidden state for
            # the current expert. We need to make sure to multiply the output hidden
            # states by `routing_weights` on the corresponding tokens (top-1 and top-2)
            current_state = hidden_states_cpu[None, top_x].reshape(-1, hidden_dim)
            current_hidden_states = expert_layer.forward(current_state) * routing_weights_cpu[top_x, idx, None]

            # However `index_add_` only support torch tensors for indexing so we'll use
            # the `top_x` tensor here.
            final_hidden_states.index_add_(0, top_x, current_hidden_states.to(hidden_states_cpu.dtype))

        return final_hidden_states