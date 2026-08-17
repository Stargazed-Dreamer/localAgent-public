"""模型对比测试：Florence-2-base vs Florence-2-large vs Qwen2.5-VL-3B
在相同的5张图片上对比 caption 质量
"""
import os
import sys
import time
from pathlib import Path

# 测试图片（从之前的测试中选取有代表性的）
TEST_IMAGES = [
    r"I:\<data_drive>:\<source_images_root>\其它来源\0_(1920×1080)\DM_20260101185824_001.png",  # 生日聚会(二次元)
    r"I:\<data_drive>:\<source_images_root>\其它来源\0_(1863×1080)\DM_20260115162633_001.jfif",  # BA蔚蓝档案(二次元)
    r"I:\<data_drive>:\<source_images_root>\其它来源\49a5fea9c674a15496e4064b403386cf_w4599_h2587_s1429.jpeg_(4599×2587)\DM_20260309200034_001.jpg",  # 瀑布森林(二次元)
]

# 确保图片存在
test_images = [p for p in TEST_IMAGES if os.path.exists(p)]
if not test_images:
    # 从DB随机取5张
    import sqlite3
    conn = sqlite3.connect(r'f:\<project_root>\output\image_organizer\files.db')
    rows = conn.execute(
        "SELECT source_path FROM files WHERE status='llm_done' ORDER BY RANDOM() LIMIT 5"
    ).fetchall()
    test_images = [r[0] for r in rows]
    conn.close()

print(f"测试图片 ({len(test_images)}):")
for p in test_images:
    print(f"  {Path(p).name}")


def test_florence(model_name):
    """测试 Florence-2 (base 或 large)"""
    print(f"\n{'='*60}")
    print(f"测试 {model_name}")
    print(f"{'='*60}")
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor
    from PIL import Image

    t0 = time.time()
    proc = AutoProcessor.from_pretrained(model_name, trust_remote_code=True, local_files_only=True)
    mdl = AutoModelForCausalLM.from_pretrained(
        model_name, torch_dtype=torch.float16, trust_remote_code=True, local_files_only=True
    ).to("cuda").eval()
    load_time = time.time() - t0
    print(f"加载耗时: {load_time:.1f}s")

    results = []
    for img_path in test_images:
        t0 = time.time()
        img = Image.open(img_path).convert("RGB")

        # CAPTION
        inputs = proc(text="<CAPTION>", images=img, return_tensors="pt")
        mi = {k: v.to("cuda", dtype=torch.float16) if k == "pixel_values" else v.to("cuda") for k, v in inputs.items()}
        with torch.inference_mode():
            ids = mdl.generate(input_ids=mi["input_ids"], pixel_values=mi["pixel_values"], max_new_tokens=96, num_beams=2, do_sample=False)
        cap_text = proc.batch_decode(ids, skip_special_tokens=False)[0]
        cap_parsed = proc.post_process_generation(cap_text, task="<CAPTION>", image_size=(img.width, img.height))

        # DETAILED_CAPTION
        inputs = proc(text="<DETAILED_CAPTION>", images=img, return_tensors="pt")
        mi = {k: v.to("cuda", dtype=torch.float16) if k == "pixel_values" else v.to("cuda") for k, v in inputs.items()}
        with torch.inference_mode():
            ids = mdl.generate(input_ids=mi["input_ids"], pixel_values=mi["pixel_values"], max_new_tokens=160, num_beams=2, do_sample=False)
        det_text = proc.batch_decode(ids, skip_special_tokens=False)[0]
        det_parsed = proc.post_process_generation(det_text, task="<DETAILED_CAPTION>", image_size=(img.width, img.height))

        # OCR
        inputs = proc(text="<OCR>", images=img, return_tensors="pt")
        mi = {k: v.to("cuda", dtype=torch.float16) if k == "pixel_values" else v.to("cuda") for k, v in inputs.items()}
        with torch.inference_mode():
            ids = mdl.generate(input_ids=mi["input_ids"], pixel_values=mi["pixel_values"], max_new_tokens=256, num_beams=2, do_sample=False)
        ocr_text = proc.batch_decode(ids, skip_special_tokens=False)[0]
        ocr_parsed = proc.post_process_generation(ocr_text, task="<OCR>", image_size=(img.width, img.height))

        elapsed = time.time() - t0
        cap = cap_parsed.get("<CAPTION>", "") if isinstance(cap_parsed, dict) else str(cap_parsed)
        det = det_parsed.get("<DETAILED_CAPTION>", "") if isinstance(det_parsed, dict) else str(det_parsed)
        ocr = ocr_parsed.get("<OCR>", "") if isinstance(ocr_parsed, dict) else str(ocr_parsed)

        print(f"\n  {Path(img_path).name} ({elapsed:.2f}s):")
        print(f"    caption: {cap}")
        print(f"    detailed: {det[:150]}...")
        print(f"    ocr: {ocr[:80] if ocr else '(无)'}")
        results.append({"image": Path(img_path).name, "caption": cap, "detailed": det, "ocr": ocr, "time": elapsed})

    # 释放模型
    del mdl, proc
    torch.cuda.empty_cache()
    return results


def test_qwen_vl():
    """测试 Qwen2.5-VL-3B-Instruct"""
    print(f"\n{'='*60}")
    print(f"测试 Qwen2.5-VL-3B-Instruct")
    print(f"{'='*60}")
    import torch
    from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor
    from PIL import Image

    t0 = time.time()
    model_name = "Qwen/Qwen2.5-VL-3B-Instruct"
    proc = AutoProcessor.from_pretrained(model_name, trust_remote_code=True, local_files_only=True)
    mdl = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        model_name, torch_dtype=torch.float16, trust_remote_code=True, local_files_only=True
    ).to("cuda").eval()
    load_time = time.time() - t0
    print(f"加载耗时: {load_time:.1f}s")

    results = []
    for img_path in test_images:
        t0 = time.time()
        img = Image.open(img_path).convert("RGB")

        # 综合描述 prompt
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": img},
                    {"type": "text", "text": "请详细描述这张图片的内容，包括：1.画面主体 2.场景 3.风格(写实/二次元/卡通等) 4.如果有文字请提取 5.如果识别出IP请说明。用简洁的段落描述。"},
                ],
            }
        ]
        text = proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = proc(text=[text], images=[img], padding=True, return_tensors="pt")
        inputs = {k: v.to("cuda") for k, v in inputs.items()}
        with torch.inference_mode():
            ids = mdl.generate(**inputs, max_new_tokens=512, do_sample=False)
        output = proc.batch_decode(ids[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]

        elapsed = time.time() - t0
        print(f"\n  {Path(img_path).name} ({elapsed:.2f}s):")
        print(f"    output: {output[:300]}...")
        results.append({"image": Path(img_path).name, "output": output, "time": elapsed})

    del mdl, proc
    torch.cuda.empty_cache()
    return results


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "florence-base"

    if model == "florence-base":
        test_florence("microsoft/Florence-2-base")
    elif model == "florence-large":
        test_florence("microsoft/Florence-2-large")
    elif model == "qwen-vl":
        test_qwen_vl()
    else:
        print(f"未知模型: {model}")
        print("用法: python test_models.py [florence-base|florence-large|qwen-vl]")
