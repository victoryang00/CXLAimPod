# """
# Description  :
# Author       : Boxin Zhang, Azure-Tang
# Version      : 0.1.0
# Copyright (c) 2024 by KVCache.AI, All Rights Reserved.
# """

# import asyncio
# import os
# import platform
# import sys
# project_dir = os.path.dirname(os.path.dirname(__file__))
# sys.path.insert(0, project_dir)
# from ktransformers.server.args import ArgumentParser


# from ktransformers.models.modeling_deepseek import DeepseekV2ForCausalLM
# from ktransformers.models.modeling_deepseek_v3 import DeepseekV3ForCausalLM
# from ktransformers.models.modeling_qwen2_moe import Qwen2MoeForCausalLM
# from ktransformers.models.modeling_llama import LlamaForCausalLM
# from ktransformers.models.modeling_mixtral import MixtralForCausalLM
# from ktransformers.server.config.config import Config

# custom_models = {
#     "DeepseekV2ForCausalLM": DeepseekV2ForCausalLM,
    # "DeepseekV3ForCausalLM": DeepseekV3ForCausalLM,
#     "Qwen2MoeForCausalLM": Qwen2MoeForCausalLM,
#     "LlamaForCausalLM": LlamaForCausalLM,
#     "MixtralForCausalLM": MixtralForCausalLM,
# }

# ktransformer_rules_dir = os.path.dirname(os.path.abspath(__file__)) + "/optimize/optimize_rules/"
# default_optimize_rules = {
#     "DeepseekV2ForCausalLM": ktransformer_rules_dir + "DeepSeek-V2-Chat-amx.yaml",
    # "DeepseekV3ForCausalLM": ktransformer_rules_dir + "DeepSeek-V3-Chat-multi-gpu-amx.yaml",
#     "Qwen2MoeForCausalLM": ktransformer_rules_dir + "Qwen2-57B-A14B-Instruct-amx.yaml",
#     "LlamaForCausalLM": ktransformer_rules_dir + "Internlm2_5-7b-Chat-1m.yaml",
#     "MixtralForCausalLM": ktransformer_rules_dir + "Mixtral.yaml",
# }


# def local_chat():
#     config = Config()
#     arg_parser = ArgumentParser(config)
#     # 初始化消息
#     arg_parser.parse_args()
#     if config.backend_type == "transformers":
#         from ktransformers.server.backend.interfaces.transformers import TransformersInterface as BackendInterface
#     elif config.backend_type == "exllamav2":
#         from ktransformers.server.backend.interfaces.exllamav2 import ExllamaInterface as BackendInterface
#     elif config.backend_type == "ktransformers":
#         from ktransformers.server.backend.interfaces.ktransformers import KTransformersInterface as BackendInterface
#     else:
#         raise NotImplementedError(f"{config.backend_type} not implemented")
#     interface = BackendInterface(config)

#     system = platform.system()
#     if system == "Windows":
#         os.system("cls")
#     else:
#         os.system("clear")
#     # add a history chat content
#     # his_content = []
#     messages = []
#     while True:
#         content = input("Chat: ")
#         if content.startswith('"""'):  # prefix """
#             # multi lines input
#             content = content[3:] + "\n"
#             while True:
#                 line = input("")
#                 if line.endswith('"""'):
#                     # end multi lines input
#                     line = line[:-3]  # suffix """
#                     if line:
#                         content += line + "\n"
#                     break
#                 else:
#                     content += line + "\n"
#         if content == "":
#             if not config.prompt_file:
#                 content = "hi"
#             else:
#                 content = open(config.prompt_file, "r").read()
#         elif os.path.isfile(content):
#             content = open(content, "r").read()
#         messages = [{"role": "user", "content": content}]

#         async def async_inference(messages):
#             generated = ""
#             async for token in interface.inference(messages, "local_chat"):
#                 generated += token
#             return generated

#         generated = asyncio.run(async_inference(messages))
#         # his_content += [
#         #     {"role": "user", "content": content},
#         #     {"role": "assistant", "content": generated},
#         # ]


# if __name__ == "__main__":
#     local_chat()


"""
Description  :  
Author       : Boxin Zhang, Azure-Tang
Version      : 0.1.0
Copyright (c) 2024 by KVCache.AI, All Rights Reserved. 
"""

import os
import platform
import sys

project_dir = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, project_dir)
import torch
import logging
from transformers import (
    AutoTokenizer,
    AutoConfig,
    AutoModelForCausalLM,
    GenerationConfig,
    TextStreamer,
)
import json
import fire
from ktransformers.optimize.optimize import optimize_and_load_gguf
from ktransformers.models.modeling_deepseek import DeepseekV2ForCausalLM
from ktransformers.models.modeling_deepseek_v3 import DeepseekV3ForCausalLM
from ktransformers.models.modeling_qwen2_moe import Qwen2MoeForCausalLM
from ktransformers.models.modeling_llama import LlamaForCausalLM
from ktransformers.models.modeling_mixtral import MixtralForCausalLM
from ktransformers.util.utils import prefill_and_generate
from ktransformers.server.config.config import Config

custom_models = {
    "DeepseekV2ForCausalLM": DeepseekV2ForCausalLM,
    "Qwen2MoeForCausalLM": Qwen2MoeForCausalLM,
    "DeepseekV3ForCausalLM": DeepseekV3ForCausalLM,
    "LlamaForCausalLM": LlamaForCausalLM,
    "MixtralForCausalLM": MixtralForCausalLM,
}

ktransformer_rules_dir = (
    os.path.dirname(os.path.abspath(__file__)) + "/optimize/optimize_rules/"
)
default_optimize_rules = {
    "DeepseekV2ForCausalLM": ktransformer_rules_dir + "DeepSeek-V2-Chat-amx.yaml",
    "DeepseekV3ForCausalLM": ktransformer_rules_dir + "DeepSeek-V3-Chat-multi-gpu-amx.yaml",
    "Qwen2MoeForCausalLM": ktransformer_rules_dir + "Qwen2-57B-A14B-Instruct-amx.yaml",
    "LlamaForCausalLM": ktransformer_rules_dir + "Internlm2_5-7b-Chat-1m.yaml",
    "MixtralForCausalLM": ktransformer_rules_dir + "Mixtral.yaml",
}


def local_chat(
    model_path: str | None = None,
    optimize_rule_path: str = None,
    gguf_path: str | None = None,
    max_new_tokens: int = 10,
    cpu_infer: int = Config().cpu_infer,
    use_cuda_graph: bool = False,
    prompt_file : str | None = None,
    mode: str = "normal",
):


    torch.set_grad_enabled(False)

    Config().cpu_infer = cpu_infer

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    config = AutoConfig.from_pretrained(model_path, trust_remote_code=True)
    if mode == 'long_context':
        assert config.architectures[0] == "LlamaForCausalLM", "only LlamaForCausalLM support long_context mode"
        torch.set_default_dtype(torch.float16)
    else:
        torch.set_default_dtype(config.torch_dtype)

    with torch.device("cpu"):
        if config.architectures[0] in custom_models:
            print("using custom modeling_xxx.py.")
            if (
                "Qwen2Moe" in config.architectures[0]
            ):  # Qwen2Moe must use flash_attention_2 to avoid overflow.
                config._attn_implementation = "flash_attention_2"
            if "Llama" in config.architectures[0]:
                config._attn_implementation = "eager"
            if "Mixtral" in config.architectures[0]:
                config._attn_implementation = "flash_attention_2"

            model = custom_models[config.architectures[0]](config)
        else:
            model = AutoModelForCausalLM.from_config(
                config, trust_remote_code=True, attn_implementation="flash_attention_2"
            )

    if optimize_rule_path is None:
        if config.architectures[0] in default_optimize_rules:
            print("using default_optimize_rule for", config.architectures[0])
            optimize_rule_path = default_optimize_rules[config.architectures[0]]
        else:
            optimize_rule_path = input(
                "please input the path of your rule file(yaml file containing optimize rules):"
            )

    if gguf_path is None:
        gguf_path = input(
            "please input the path of your gguf file(gguf file in the dir containing input gguf file must all belong to current model):"
        )
    optimize_and_load_gguf(model, optimize_rule_path, gguf_path, config)

    model.generation_config = GenerationConfig.from_pretrained(model_path)
    if model.generation_config.pad_token_id is None:
        model.generation_config.pad_token_id = model.generation_config.eos_token_id
    model.eval()
    try:
        import intel_extension_for_pytorch as ipex
        model = ipex.optimize(
            model,
            dtype=torch.bfloat16,   # 触发 BF16 + AMX
            inplace=True,
        )
    except:
        pass
    logging.basicConfig(level=logging.INFO)

    system = platform.system()
    if system == "Windows":
        os.system("cls")
    else:
        os.system("clear")

    while True:
        content = input("Chat: ")
        if content.startswith('"""'):  # prefix """
            # multi lines input
            content = content[3:] + "\n"
            while True:
                line = input("")
                if line.endswith('"""'):
                    # end multi lines input
                    line = line[:-3]  # suffix """
                    if line:
                        content += line + "\n"
                    break
                else:
                    content += line + "\n"
        if content == "":
            if prompt_file != None:
                content = open(prompt_file, "r").read()
            else:
                content = "Please write a piece of quicksort code in C++."
        elif os.path.isfile(content):
            content = open(content, "r").read()
        messages = [{"role": "user", "content": content}]
        input_tensor = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt"
        )
        if mode == 'long_context':
            assert Config().long_context_config['max_seq_len'] > input_tensor.shape[1] + max_new_tokens, \
            "please change max_seq_len in  ~/.ktransformers/config.yaml"
        torch.set_default_dtype(
            torch.bfloat16
        )  # TODO: Remove this, replace dtype using config
        generated = prefill_and_generate(
            model, tokenizer, input_tensor, max_new_tokens, use_cuda_graph, mode
        )
        #return


if __name__ == "__main__":
    fire.Fire(local_chat)