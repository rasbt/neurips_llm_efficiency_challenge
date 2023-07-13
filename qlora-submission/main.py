from typing import Union
from pathlib import Path
from fastapi import FastAPI

import logging

# API imports
from api import (
    ProcessRequest,
    ProcessResponse,
    TokenizeRequest,
    TokenizeResponse,
    Token,
)

# Lit-llama imports
import sys
import time
from pathlib import Path

import torch

# support running without installing as a package
wd = Path(__file__).parent.parent.resolve()
sys.path.append(str(wd))

from qlora.qlora import get_last_checkpoint
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, GenerationConfig
from peft import PeftModel

app = FastAPI()

logger = logging.getLogger(__name__)
# Configure the logging module
logging.basicConfig(level=logging.INFO)


# adapter_path, _ = get_last_checkpoint('qlora/output/guanaco-7b')
adapter_path = 'timdettmers/guanaco-7b' # If you want to skip training, you can jsut load this model from the Hub.
model_name_or_path = 'huggyllama/llama-7b'

t0 = time.time()
logger.info("Loading model ...")

# Load the model (use bf16 for faster inference)
model = AutoModelForCausalLM.from_pretrained(
    model_name_or_path,
    torch_dtype=torch.bfloat16,
    device_map={"": 0}
)
model = PeftModel.from_pretrained(model, adapter_path)

logger.info(f"Time to load model: {time.time() - t0:.02f} seconds.")

model.eval()

tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
tokenizer.bos_token_id = 1

@app.post("/process")
async def process_request(input_data: ProcessRequest) -> ProcessResponse:
    inputs = tokenizer(input_data.prompt, return_tensors="pt").to('cuda')

    t0 = time.perf_counter()

    outputs = model.generate(
        **inputs, 
        generation_config=GenerationConfig(
            do_sample=True,
            max_new_tokens=input_data.max_new_tokens,
            temperature=input_data.temperature,
            top_k=input_data.top_k,
            output_scores=True,
            return_dict_in_generate=True,
        )
    )
    t = time.perf_counter() - t0
    
    # Extract tokens
    generated_ids = outputs.sequences[0][-len(outputs.scores):]
    text = tokenizer.decode(generated_ids, skip_special_tokens=True)
    tokens = tokenizer.convert_ids_to_tokens(generated_ids, skip_special_tokens=True)

    # Logprobs of the sampled tokens
    log_softmax = torch.nn.LogSoftmax(dim=0)
    logprobs_full = [log_softmax(score[0]) for score in outputs.scores]
    logprobs = [logprobs_full[gen_id][tok_id] for gen_id, tok_id in enumerate(generated_ids)]

    # Top logprob for every token (even when not selected in generation)
    top_logprobs = [
        (torch.argmax(logprob), logprob[torch.argmax(logprob)]) for logprob in logprobs_full
    ]

    tokens_generated = len(tokens)
    logger.info(
        f"Time for inference: {t:.02f} sec total, {tokens_generated / t:.02f} tokens/sec"
    )
    logger.info(f"Memory used: {torch.cuda.max_memory_reserved() / 1e9:.02f} GB")
    ret_tokens = []
    for tok, lp, tlp in zip(tokens, logprobs, top_logprobs):
        idx, val = tlp
        tok_str = tokenizer.decode(idx)
        token_tlp = {tok_str: float(val.item())}
        ret_tokens.append(
            Token(text=tok, logprob=float(lp.item()), top_logprob=token_tlp)
        )
    logprobs_sum = sum(logprobs)
    # Process the input data here
    return ProcessResponse(
        text=text, tokens=ret_tokens, logprob=logprobs_sum, request_time=t
    )


@app.post("/tokenize")
async def tokenize(input_data: TokenizeRequest) -> TokenizeResponse:
    t0 = time.perf_counter()
    inputs = tokenizer(input_data.text, max_length=input_data.max_length, truncation=input_data.truncation)
    t = time.perf_counter() - t0
    return TokenizeResponse(tokens=inputs.input_ids, request_time=t)
