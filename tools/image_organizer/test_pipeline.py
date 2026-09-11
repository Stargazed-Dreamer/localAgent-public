"""集成测试：Florence-2(cap+ocr) + LLM池分类，验证全流程。
用 cuda10 conda 环境运行：
  <data_drive>:\<miniconda_root>\\envs\\cuda10\\python.exe temp\test_pipeline.py
"""
import json
import time
from pathlib import Path

import requests
import torch
from PIL import Image
from transformers import AutoModelForCausalLM, AutoProcessor

API = "http://127.0.0.1:8766"
PROJECT = "图片整理测试"

# 13 类主分类
CATEGORIES = {
    1: "二次元-角色立绘", 2: "二次元-场景插画", 3: "二次元-漫画条漫",
    4: "梗图搞笑", 5: "表情包", 6: "壁纸",
    7: "生活-人物照", 8: "生活-风景场景", 9: "社媒截图",
    10: "游戏截图", 11: "绘画参考", 12: "设计素材", 13: "无法分类",
}

SYSTEM_PROMPT = """你是图片分类与标签提取引擎。根据图片的路径、文件名、图像描述(caption)和OCR文字，输出结构化分类。

## 主分类（必须选一个编号）
1.二次元-角色立绘 2.二次元-场景插画 3.二次元-漫画条漫 4.梗图搞笑 5.表情包 6.壁纸 7.生活-人物照 8.生活-风景场景 9.社媒截图 10.游戏截图 11.绘画参考 12.设计素材 13.无法分类

## 多维度标签（能填就填，无则留空数组或空串）
- scene: 场景(室内/室外/演出现场/教室/自然风景/街头等)
- subject: 主体(人物/动物/物品/风景/文字等)
- props: 道具列表(吉他/电脑/手机/武器等)
- activity: 活动(音乐/运动/学习/工作等)
- style: 风格(写实/二次元/卡通/像素/油画等)
- mood: 情绪(欢乐/悲伤/温馨/紧张等)
- ip: 来源IP(明日方舟/原神等，无则空)
- reference: 绘画参考维度(姿势/视角/构图/光影/人体/色彩等，适合学画参考的维度)
- colors: 主色调(暖色/冷色/黑白/鲜艳/低饱和等)

## 输出格式
严格输出一行JSON(不要markdown，不要多余文字)：
{"main_category":"二次元-角色立绘","main_category_id":1,"dimensions":{"scene":"...","subject":"...","props":[],"activity":"...","style":"...","mood":"...","ip":"...","reference":"...","colors":"..."},"tags":["标签1","标签2"],"confidence":0.8}

若内容涉及色情/违规被拒绝，输出 {"main_category":"无法分类","main_category_id":13,"refused":true,"dimensions":{},"tags":["审查拒绝"],"confidence":0}
若信息严重不足无法判断，main_category_id=13。"""

# 5 张代表性测试图
TEST_IMAGES = [
    r"I:\<data_drive>:\<source_images_root>\森空岛\白金\DM_20251128163046_001.webp",
    r"I:\<data_drive>:\<classified_root>\图片\搞笑图片\5aff86666667b.jpg",
    r"G:\<data_drive>:\<ipad_backup>\ipad\100APPLE\IMG_0041.JPG",
    r"I:\<data_drive>:\<classified_root>\图片-Picture\4 壁纸\3\568.jpg",
    r"I:\<data_drive>:\<classified_root>\图片\毕业照\同学照\丁沁菲.jpg",
]

def load_florence():
    print("Loading Florence-2-base...")
    t0 = time.time()
    proc = AutoProcessor.from_pretrained("microsoft/Florence-2-base", trust_remote_code=True, local_files_only=True)
    mdl = AutoModelForCausalLM.from_pretrained("microsoft/Florence-2-base", torch_dtype=torch.float16, trust_remote_code=True, local_files_only=True).to("cuda").eval()
    print(f"Loaded in {time.time()-t0:.1f}s")
    return proc, mdl

def run_task(proc, mdl, image, task_prompt, max_tokens=128):
    inputs = proc(text=task_prompt, images=image, return_tensors="pt")
    mi = {}
    for k, v in inputs.items():
        mi[k] = v.to(device="cuda", dtype=torch.float16) if k == "pixel_values" else v.to("cuda")
    with torch.inference_mode():
        ids = mdl.generate(input_ids=mi["input_ids"], pixel_values=mi["pixel_values"], max_new_tokens=max_tokens, num_beams=2, do_sample=False)
    text = proc.batch_decode(ids, skip_special_tokens=False)[0]
    parsed = proc.post_process_generation(text, task=task_prompt, image_size=(image.width, image.height))
    return parsed

def get_caption_ocr(proc, mdl, img_path):
    img = Image.open(img_path).convert("RGB")
    cap = run_task(proc, mdl, img, "<CAPTION>", 96)
    ocr = run_task(proc, mdl, img, "<OCR>", 256)
    cap_text = cap.get("<CAPTION>", "") if isinstance(cap, dict) else str(cap)
    ocr_text = ocr.get("<OCR>", "") if isinstance(ocr, dict) else str(ocr)
    return cap_text.strip(), ocr_text.strip()

def call_llm(messages, max_tokens=65536, timeout=120):
    payload = {"messages": messages, "temperature": 0.2, "max_tokens": max_tokens, "timeout": 100, "retries": 3, "project": PROJECT}
    for attempt in range(3):
        try:
            r = requests.post(f"{API}/llm/pool/call", json=payload, timeout=timeout)
            if r.status_code != 200:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
                time.sleep(5)
                continue
            data = r.json()
            if data.get("ok"):
                return data.get("content", ""), None
            return None, data.get("error", "unknown")
        except Exception as e:
            print(f"  Exception: {e}")
            time.sleep(5)
    return None, "max retries"

def parse_json(text):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"): lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e+1])
        except Exception:
            pass
    return None

def main():
    # 1. 检查后端 LLM 池
    print("=== 检查后端 LLM 池 ===")
    try:
        r = requests.get(f"{API}/llm/pool/status", timeout=10)
        st = r.json()
        print(f"  pool ok: {st.get('ok')}, keys: {st.get('total_keys', '?')}, active: {st.get('active_keys', '?')}")
        print(f"  model: {st.get('model', '?')}")
    except Exception as e:
        print(f"  后端不可用: {e}")
        return

    # 2. 加载 Florence-2
    proc, mdl = load_florence()

    # 3. 对每张图跑 cap+ocr + LLM 分类
    print(f"\n=== 处理 {len(TEST_IMAGES)} 张测试图 ===")
    for img_path in TEST_IMAGES:
        p = Path(img_path)
        if not p.exists():
            print(f"\n[跳过] 不存在: {img_path}")
            continue
        print(f"\n--- {p.name} ---")
        print(f"  路径: {p.parent}")

        # Florence-2
        t0 = time.time()
        cap, ocr = get_caption_ocr(proc, mdl, img_path)
        dt = time.time() - t0
        print(f"  caption: {cap}")
        print(f"  ocr: {ocr[:100] if ocr else '(无)'}")
        print(f"  Florence耗时: {dt:.2f}s")

        # LLM 分类
        user_msg = f"""## 文件路径
{p.parent.name}

## 文件名
{p.name}

## 图像描述(caption)
{cap}

## OCR文字
{ocr if ocr else '(无)'}"""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_msg}]
        t0 = time.time()
        content, err = call_llm(messages)
        dt = time.time() - t0
        if err:
            print(f"  LLM错误: {err}")
            continue
        print(f"  LLM耗时: {dt:.2f}s")
        result = parse_json(content)
        if result:
            cat_id = result.get("main_category_id", 13)
            cat_name = CATEGORIES.get(cat_id, "未知")
            print(f"  分类: {cat_name} (id={cat_id})")
            print(f"  维度: {json.dumps(result.get('dimensions', {}), ensure_ascii=False)}")
            print(f"  标签: {result.get('tags', [])}")
            print(f"  置信度: {result.get('confidence', '?')}")
        else:
            print(f"  解析失败: {content[:200] if content else '(空)'}")

if __name__ == "__main__":
    main()
