"""Florence-2 测速脚本（独立，不依赖 picture_tagger 包）。
用 cuda10 conda 环境运行：
  E:\<data_drive>:\<miniconda_root>\envs\cuda10\python.exe temp\florence_bench.py
"""
import time
import sys
import torch
from transformers import AutoModelForCausalLM, AutoProcessor
from PIL import Image
from pathlib import Path

print(f"torch={torch.__version__}, cuda={torch.cuda.is_available()}, gpu={torch.cuda.get_device_name(0)}")
print(f"GPU mem free: {torch.cuda.mem_get_info()[0]/1024**3:.2f} GB / {torch.cuda.mem_get_info()[1]/1024**3:.2f} GB")

print("\nLoading Florence-2-base...")
t0 = time.time()
processor = AutoProcessor.from_pretrained("microsoft/Florence-2-base", trust_remote_code=True, local_files_only=True)
model = AutoModelForCausalLM.from_pretrained(
    "microsoft/Florence-2-base", torch_dtype=torch.float16, trust_remote_code=True, local_files_only=True
).to("cuda").eval()
print(f"Model loaded in {time.time()-t0:.1f}s")
print(f"GPU mem after load: {torch.cuda.mem_get_info()[0]/1024**3:.2f} GB free")


def run_task(image, task_prompt, max_tokens=128):
    inputs = processor(text=task_prompt, images=image, return_tensors="pt")
    mi = {}
    for k, v in inputs.items():
        if k == "pixel_values":
            mi[k] = v.to(device="cuda", dtype=torch.float16)
        else:
            mi[k] = v.to("cuda")
    with torch.inference_mode():
        ids = model.generate(
            input_ids=mi["input_ids"], pixel_values=mi["pixel_values"],
            max_new_tokens=max_tokens, num_beams=2, do_sample=False,
        )
    text = processor.batch_decode(ids, skip_special_tokens=False)[0]
    parsed = processor.post_process_generation(text, task=task_prompt, image_size=(image.width, image.height))
    return parsed


test_dir = Path(r"F:\codex\PictureTagger\test")
files = sorted([f for f in test_dir.iterdir() if f.suffix.lower() in ('.jpg', '.png', '.jpeg', '.webp')])
print(f"\nFound {len(files)} test images")

# warmup
print("Warmup...")
img = Image.open(files[0]).convert("RGB")
run_task(img, "<CAPTION>")
print("Warmup done\n")

# test 3 tasks (caption + detailed + ocr)
n = min(15, len(files) - 1)
times = []
for f in files[1:1 + n]:
    img = Image.open(f).convert("RGB")
    t0 = time.time()
    run_task(img, "<CAPTION>", 96)
    run_task(img, "<DETAILED_CAPTION>", 160)
    run_task(img, "<OCR>", 256)
    dt = time.time() - t0
    times.append(dt)
    print(f"  3tasks {f.name[:40]:40s} {dt:.2f}s")
avg = sum(times) / len(times)
print(f"\n=== 3 tasks: avg {avg:.2f}s/img, est 55657: {55657*avg/3600:.1f}h ===")

# test caption+ocr only (skip detailed)
times2 = []
for f in files[1:1 + n]:
    img = Image.open(f).convert("RGB")
    t0 = time.time()
    run_task(img, "<CAPTION>", 96)
    run_task(img, "<OCR>", 256)
    dt = time.time() - t0
    times2.append(dt)
avg2 = sum(times2) / len(times2)
print(f"=== cap+ocr: avg {avg2:.2f}s/img, est 55657: {55657*avg2/3600:.1f}h ===")

# test caption only
times3 = []
for f in files[1:1 + n]:
    img = Image.open(f).convert("RGB")
    t0 = time.time()
    run_task(img, "<CAPTION>", 96)
    dt = time.time() - t0
    times3.append(dt)
avg3 = sum(times3) / len(times3)
print(f"=== caption only: avg {avg3:.2f}s/img, est 55657: {55657*avg3/3600:.1f}h ===")
