"""交互式OCR坐标缩放调试工具 - 拖动滑块调整sx/sy，实时预览"""
import requests
import base64
import io
import json
from PIL import Image, ImageDraw, ImageFont, ImageTk
import tkinter as tk
from tkinter import ttk

API = "http://127.0.0.1:8766"
WINDOW = "异环  "

class OCRScaleDebugger:
    def __init__(self, root):
        self.root = root
        self.root.title("OCR坐标缩放调试")
        self.root.geometry("1400x900")

        # 加载数据
        self.load_data()

        # 控制面板
        ctrl = ttk.Frame(root)
        ctrl.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)

        ttk.Label(ctrl, text="sx:").pack(side=tk.LEFT)
        self.sx_var = tk.DoubleVar(value=1.0)
        self.sx_slider = ttk.Scale(ctrl, from_=0.50, to=1.20, variable=self.sx_var,
                                    orient=tk.HORIZONTAL, length=300, command=self.on_change)
        self.sx_slider.pack(side=tk.LEFT, padx=5)
        self.sx_label = ttk.Label(ctrl, text="1.00", width=5)
        self.sx_label.pack(side=tk.LEFT)

        ttk.Label(ctrl, text="  sy:").pack(side=tk.LEFT)
        self.sy_var = tk.DoubleVar(value=1.0)
        self.sy_slider = ttk.Scale(ctrl, from_=0.50, to=1.20, variable=self.sy_var,
                                    orient=tk.HORIZONTAL, length=300, command=self.on_change)
        self.sy_slider.pack(side=tk.LEFT, padx=5)
        self.sy_label = ttk.Label(ctrl, text="1.00", width=5)
        self.sy_label.pack(side=tk.LEFT)

        ttk.Button(ctrl, text="重新截图", command=self.load_data).pack(side=tk.LEFT, padx=20)
        ttk.Button(ctrl, text="保存当前参数", command=self.save_params).pack(side=tk.LEFT, padx=5)

        # 图片显示
        self.canvas = tk.Canvas(root, bg="black")
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 首次渲染延迟等canvas展开
        self.root.after(200, self.render)

    def load_data(self):
        print("截图+OCR中...")
        resp = requests.post(f"{API}/screen/capture", json={"mode": "fullscreen"})
        data = resp.json()
        img_data = base64.b64decode(data["image"])
        self.img = Image.open(io.BytesIO(img_data))
        self.W, self.H = self.img.size
        self.CX, self.CY = self.W // 2, self.H // 2

        ocr_resp = requests.post(f"{API}/ocr/base64", data={"data": data["image"]})
        ocr_data = ocr_resp.json()
        self.details = ocr_data.get("details", [])
        print(f"截图 {self.W}x{self.H}, OCR {len(self.details)} 个文本块")

    def on_change(self, _=None):
        self.sx_label.config(text=f"{self.sx_var.get():.2f}")
        self.sy_label.config(text=f"{self.sy_var.get():.2f}")
        self.render()

    def render(self):
        sx = self.sx_var.get()
        sy = self.sy_var.get()

        img = self.img.copy()
        draw = ImageDraw.Draw(img)

        try:
            font = ImageFont.truetype("arial.ttf", 14)
        except Exception:
            font = ImageFont.load_default()

        # 画中心十字
        draw.line([(self.CX-20, self.CY), (self.CX+20, self.CY)], fill="yellow", width=1)
        draw.line([(self.CX, self.CY-20), (self.CX, self.CY+20)], fill="yellow", width=1)

        for item in self.details:
            text = item.get("text", "")
            box = item.get("box", [])
            if box and len(box) >= 4:
                # 原始OCR坐标
                orig_pts = [(p[0], p[1]) for p in box]

                # 缩放后坐标
                scaled_pts = []
                for px, py in orig_pts:
                    new_x = self.CX + (px - self.CX) * sx
                    new_y = self.CY + (py - self.CY) * sy
                    scaled_pts.append((int(new_x), int(new_y)))

                # 红色=原始
                draw.polygon([(int(p[0]), int(p[1])) for p in orig_pts], outline="red", width=1)
                # 绿色=缩放后
                draw.polygon(scaled_pts, outline="lime", width=2)

                # 缩放后中心
                cx = sum(p[0] for p in scaled_pts) / 4
                cy = sum(p[1] for p in scaled_pts) / 4
                draw.ellipse([cx-3, cy-3, cx+3, cy+3], fill="lime")

                short = text[:10] if len(text) > 10 else text
                draw.text((cx+6, cy-8), short, fill="yellow", font=font)

        # 缩放显示
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        if canvas_w < 10 or canvas_h < 10:
            canvas_w, canvas_h = 1380, 800
        scale = min(canvas_w / self.W, canvas_h / self.H)
        new_w = int(self.W * scale)
        new_h = int(self.H * scale)

        display = img.resize((new_w, new_h), Image.LANCZOS)
        self.tk_img = ImageTk.PhotoImage(display)

        self.canvas.delete("all")
        self.canvas.create_image(canvas_w//2, canvas_h//2, image=self.tk_img)

    def save_params(self):
        params = {"sx": round(self.sx_var.get(), 4), "sy": round(self.sy_var.get(), 4)}
        with open("temp/ocr_scale_params.json", "w") as f:
            json.dump(params, f, indent=2)
        print(f"已保存参数: {params}")

if __name__ == "__main__":
    root = tk.Tk()
    app = OCRScaleDebugger(root)
    root.mainloop()
