# html_toolkit - HTML 生成工具栈

> 整合自 anthropics/skills 的 6 个 HTML 相关 skill：algorithmic-art / brand-guidelines / canvas-design / frontend-design / web-artifacts-builder / theme-factory。
> 去重整合后形成一份完整的 HTML 生成参考。

## 1. 概述

本文件整合 anthropics/skills 中 6 个 HTML 相关 skill 的精华，去除重叠内容，按"为什么 / 怎么做 / 反模式"组织。

**适用场景**：
- 新项目需要生成 HTML 页面（Dashboard、工具页、报告页、可视化）
- 需要在网页里画 Canvas / WebGL 艺术
- 需要为应用定义主题系统（CSS 变量、设计令牌）
- 需要把 React/Vue 单页应用打包成单文件 HTML

**不适用场景**（LocalAgent 项目自身的选择）：
- 本项目用 PySide6 做桌面 GUI（`client/`），不走 Web 路线
- 嵌入式 / 移动端原生应用

**核心精神**（来自 frontend-design）：
> AI 生成的设计现在聚类在三种 look：(1) 暖米色背景配高对比衬线显示字体 + 赤陶色强调；(2) 近黑背景配单一酸绿或朱红强调；(3) 报纸式布局配发丝分割线和零圆角。这三种都是**默认**而非**选择**——任何 brief 都会产出来。brief 没指明时，不要把自由度花在这些默认上。

## 2. 单文件 HTML 架构原则

来自 web-artifacts-builder + 本项目实践。

### 2.1 为什么要单文件

- **可移植**：一个 `.html` 文件双击就能在任意浏览器打开，无需 npm install
- **可分享**：可直接贴到聊天 / 邮件 / 文档
- **无构建工具依赖**：内联 CSS/JS，仅外部 CDN 引用必须的库（p5.js / Tailwind / React）
- **可存档**：单文件比一坨源码目录更适合长期归档

### 2.2 单文件 HTML 骨架

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>页面标题</title>
  <!-- 外部 CDN：仅必需的，不堆砌 -->
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- 或 p5.js / React / Vue，按需引入 -->
  <style>
    /* 所有 CSS 内联 */
    :root {
      --color-primary: #2e7d32;  /* 绿色主色 */
      --color-bg: #faf9f5;
      --color-text: #141413;
      --radius-sm: 4px;
      --radius-md: 8px;
    }
    body { font-family: -apple-system, "PingFang SC", sans-serif; }
  </style>
</head>
<body>
  <!-- HTML 结构 -->
  <main>
    <h1>主标题</h1>
    <p>正文内容</p>
  </main>
  <script>
    // 所有 JS 内联
    document.addEventListener('DOMContentLoaded', () => {
      // 初始化逻辑
    });
  </script>
</body>
</html>
```

### 2.3 React/Vue SPA 打包成单文件

参考 web-artifacts-builder 的 `bundle-artifact.sh`：
1. 用 Vite 开发
2. 用 Parcel 打包（`parcel build` + `html-inline` 内联所有资源）
3. 输出 `bundle.html`——所有 JS/CSS 内联

**何时打包**：交付时才打包；开发期保持源码结构（Vite dev server 热重载）

## 3. anti-AI-slop 设计原则

来自 frontend-design + web-artifacts-builder + 本项目实战。

### 3.1 七大反模式（必须避免）

| # | 反模式 | 为什么是 slop | 正确做法 |
|---|--------|--------------|---------|
| 1 | **过度居中**（一切居中） | 视觉呆板、无层级、无方向感 | 主轴对齐 + 次轴左对齐；用列宽和留白制造节奏 |
| 2 | **紫色渐变**（紫蓝 / 紫粉） | AI 默认色板，所有模型都偏这个 | 选与主题相关的具象色（如绿色对应自然/医疗，赤陶对应陶艺） |
| 3 | **统一圆角**（所有元素 8px / 12px） | 无差别，丢失层级 | 区分：图直角 / 卡片 8px / 按钮 4px / 输入框 2px |
| 4 | **Inter 字体**（无衬线、Inter 单一字族） | AI 默认字体，毫无个性 | 配对：display 用衬线 / grotesk / mono；body 用 Lora / Source Serif / 思源 |
| 5 | **emoji 堆砌**（🚀✨🎯 当图标） | 没设计图标时的偷懒 | 用 SVG icon 或字体图标（Lucide / Heroicons） |
| 6 | **均匀间距**（所有 margin/padding 16px） | 无视觉重点、无呼吸 | 8/16/24/40 多档间距；主标题留白 > 段落 > 行内 |
| 7 | **纯文本骨架**（只有 h1+p+ul） | 像论文不像界面 | 加分隔线 / 编号眉标 / 数据卡 / 引文块 |

### 3.2 三种 AI 默认 look（要主动跳出）

来自 frontend-design：
1. **暖米色背景（#F4F1EA）+ 高对比衬线显示字 + 赤陶强调** —— 适合陶艺 / 工艺品 brief，但不该是默认
2. **近黑背景 + 酸绿或朱红单一强调** —— 适合 cyber / tech brief，但被滥用
3. **报纸式布局 + 发丝分割线 + 零圆角 + 密集列** —— 适合新闻 / 报告 brief，但不该到处用

### 3.3 Dominance over Equality（主色 60-70%）

设计要有主从：主色占 60-70%，副色 20-30%，强调色 5-10%。三色均分 = AI slop。

```css
/* 正确：主色主导 */
:root {
  --color-primary: #2e7d32;      /* 主色 70%：背景大面积 / 主操作 */
  --color-secondary: #788c5d;    /* 副色 20%：分隔 / 次要元素 */
  --color-accent: #d97757;       /* 强调 10%：CTA / 高亮 */
  --color-bg: #faf9f5;
  --color-text: #141413;
}
```

### 3.4 设计 risk：每个 brief 至少冒一个险

来自 frontend-design：
> 让 signature 元素是唯一让人记住的东西，其他保持克制纪律。**不冒险本身也可能是冒险**——做成默认样子等于没做。

**signature 例子**：
- 一个独特的字体处理（如把品牌首字母做成大号下沉首字母）
- 一个非典型的色彩组合（如医疗用品用陶土橙 + 苔藓绿）
- 一个手工感的装饰元素（如手绘 SVG 分隔符）
- 一个交互时刻（如滚动触发的有节奏揭示）

### 3.5 主动自检清单

写完 HTML 后问自己：
- [ ] 我做的设计会不会用给"任何相似 brief"都产出一样？会 → 重做
- [ ] 我用的字体组合是不是我"任何项目都会伸手拿"的？是 → 换
- [ ] 我用的颜色组合是不是当前主流 AI 模型的默认产物？是 → 换
- [ ] 我有没有冒一个险？没有 → 加一个 signature 元素
- [ ] 我有没有过度装饰？有 → 删（"离开前照镜子，去掉一件配饰"——Coco Chanel）

## 4. 品牌指南（brand-guidelines）

来自 anthropics/skills 的 brand-guidelines。原 skill 是为 Anthropic 内部品牌定制的，这里抽象成可复用模板。

### 4.1 色彩规范模板

```css
:root {
  /* 主色：用于背景大面积 / 主操作按钮 / 主标题 */
  --color-primary: #2e7d32;        /* 主色 */
  --color-primary-hover: #1b5e20;  /* hover 态 */
  --color-primary-light: #e8f5e9;  /* 浅色背景 */

  /* 副色：分隔线 / 次要元素 / 标签 */
  --color-secondary: #788c5d;
  --color-secondary-light: #e8e6dc;

  /* 强调色：CTA / 高亮 / 重要数字 */
  --color-accent: #d97757;         /* 橙 */
  /* 或：--color-accent: #6a9bcc;  /* 蓝 */
  /* 或：--color-accent: #788c5d;  /* 绿 */

  /* 中性色 */
  --color-text: #141413;           /* 主文字 */
  --color-text-muted: #b0aea5;     /* 次要文字 */
  --color-bg: #faf9f5;              /* 主背景 */
  --color-bg-dark: #141413;         /* 深色背景（暗模式） */
  --color-border: #e8e6dc;          /* 边框 */
}
```

### 4.2 字体规范模板

```css
:root {
  /* 标题：用 characterful display 字体（衬线 / grotesk / mono） */
  --font-display: "Poppins", "PingFang SC", -apple-system, sans-serif;
  /* 备选：Lora / Source Serif Pro / Playfair Display / IBM Plex Serif */

  /* 正文：易读的 body 字体 */
  --font-body: "Lora", "Source Han Serif SC", Georgia, serif;
  /* 备选：Inter / IBM Plex Sans / Source Sans Pro */

  /* 等宽：用于代码 / 数据 */
  --font-mono: "JetBrains Mono", "Fira Code", Consolas, monospace;

  /* 字号尺度（modular scale） */
  --text-xs: 0.75rem;    /* 12px */
  --text-sm: 0.875rem;   /* 14px */
  --text-base: 1rem;     /* 16px */
  --text-lg: 1.125rem;   /* 18px */
  --text-xl: 1.25rem;    /* 20px */
  --text-2xl: 1.5rem;    /* 24px */
  --text-3xl: 1.875rem;  /* 30px */
  --text-4xl: 2.25rem;   /* 36px */
  --text-5xl: 3rem;      /* 48px */
  --text-6xl: 3.75rem;   /* 60px */
}
```

**字体配对原则**：
- 一个 characterful display + 一个易读 body + 一个 mono
- 不要都用同一个字族（如全是 Inter）—— 那是 AI slop
- 中文项目：display 用 PingFang SC / Source Han Sans；body 用 Source Han Serif / Lora + 中文备选
- 字体预装好；提供 fallback（系统字体）

### 4.3 间距规范模板

```css
:root {
  /* 间距尺度（4 的倍数） */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-6: 24px;
  --space-8: 32px;
  --space-12: 48px;
  --space-16: 64px;
  --space-24: 96px;

  /* 圆角：区分层级 */
  --radius-sm: 2px;    /* 输入框 / 小标签 */
  --radius-md: 4px;    /* 按钮 */
  --radius-lg: 8px;    /* 卡片 */
  --radius-full: 9999px; /* 胶囊 */

  /* 阴影 */
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.05);
  --shadow-md: 0 4px 6px rgba(0,0,0,0.1);
  --shadow-lg: 0 10px 15px rgba(0,0,0,0.1);
}
```

**间距原则**：
- 不要所有元素都用 16px——那是 AI slop
- 主标题留白 > 段落留白 > 行内留白（节奏感）
- 章节间用 48-64px；段落间 16-24px；行内 8-12px

## 5. Canvas 设计要点

来自 canvas-design + algorithmic-art。

### 5.1 HiDPI 处理（必做）

```javascript
function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  canvas.style.width = rect.width + 'px';
  canvas.style.height = rect.height + 'px';
  return ctx;
}
```

不处理 HiDPI 的后果：Retina 屏上画 1px 线变成 2px 模糊线，看起来业余。

### 5.2 坐标系统

- Canvas 原点 (0,0) 在**左上角**（不是数学坐标系左下）
- Y 轴向下为正
- 旋转：顺时针为正（数学坐标系逆时针为正，容易混淆）
- p5.js 默认也是左上原点，但可用 `translate()`/`rotate()`/`scale()` 改变

### 5.3 性能优化

- **离屏缓冲**：静态层画到 offscreen canvas，每帧只重绘动态层
- **批渲染**：相同 fillStyle 的形状合并到一次 beginPath
- **避免每帧分配**：粒子数组复用，不要每帧 `new`
- **requestAnimationFrame**：用 rAF 而非 setInterval
- **节流重绘**：低频更新（如时钟）用 setTimeout 1s 而非 60fps rAF

### 5.4 Canvas vs SVG vs WebGL

| 技术 | 适用 | 不适用 |
|------|------|--------|
| Canvas 2D | 粒子 / 流场 / 实时动画 / 像素操作 | 矢量图标 / 可访问性要求高 |
| SVG | 图标 / 图表 / 可缩放矢量 | 大量元素（>1000 性能差） |
| WebGL | 3D / 复杂 shader / 大规模粒子（10w+） | 简单 2D（杀鸡用牛刀） |

## 6. 算法艺术基础

来自 algorithmic-art。

### 6.1 p5.js 还是原生 Canvas

- **p5.js**：快速原型、有现成的 noise/random/向量数学、社区作品多
- **原生 Canvas**：性能极致、无依赖、控制精细

新项目优先 p5.js（开发快）；性能瓶颈时迁原生 Canvas。

### 6.2 Seeded Randomness（艺术块模式）

```javascript
// 始终用 seed 保证可复现——同一 seed 同一输出
let seed = 12345;
randomSeed(seed);
noiseSeed(seed);

// 用户可调 seed 生成变体
function regenerate(newSeed) {
  seed = newSeed;
  randomSeed(seed);
  noiseSeed(seed);
  redraw();
}
```

### 6.3 噪声（Perlin / Simplex）

```javascript
// Perlin 噪声：平滑的伪随机
let n = noise(x * 0.01, y * 0.01, t * 0.005);
// 输出 [0, 1]，可映射到任意范围
let angle = n * TWO_PI;
let radius = map(n, 0, 1, 50, 200);
```

**噪声参数调参**：
- `x * 0.01` 中的 0.01 是缩放——值小=大块平滑、值大=细密噪点
- 第三维 `t * 0.005` 是时间维度——做动画用
- 多层叠加（octaves）= 山脉/云彩等自然纹理

### 6.4 粒子系统

```javascript
class Particle {
  constructor() {
    this.pos = createVector(random(width), random(height));
    this.vel = createVector(0, 0);
    this.acc = createVector(0, 0);
    this.maxSpeed = 2;
  }

  follow(flowField) {
    // 跟随流场向量
    const x = floor(this.pos.x / scl);
    const y = floor(this.pos.y / scl);
    const force = flowField[x][y];
    this.applyForce(force);
  }

  update() {
    this.vel.add(this.acc);
    this.vel.limit(this.maxSpeed);
    this.pos.add(this.vel);
    this.acc.mult(0);
  }

  edges() {
    // 环绕边界
    if (this.pos.x > width) this.pos.x = 0;
    if (this.pos.x < 0) this.pos.x = width;
    // ...
  }
}
```

### 6.5 五种算法艺术哲学（来自 anthropic）

来自 algorithmic-art 的哲学示例，可作创意起点：

| 哲学名 | 核心 | 算法表达 |
|--------|------|----------|
| **Organic Turbulence** | 自然律约束下的混沌 | 流场驱动粒子，速度→颜色 |
| **Quantum Harmonics** | 离散实体显示波干涉 | 网格粒子带相位，干涉产生亮暗节点 |
| **Recursive Whispers** | 跨尺度的自相似 | L-system / 递归分支 + 黄金比 |
| **Field Dynamics** | 不可见力通过物质效应可见化 | 向量场 + 粒子轨迹 |
| **Stochastic Crystallization** | 随机过程结晶为有序 | 圆形 packing / Voronoi 松弛 |

### 6.6 单文件 p5.js 骨架

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Generative Art</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.7.0/p5.min.js"></script>
  <style>
    body { margin: 0; background: #141413; }
    #canvas-container { display: flex; justify-content: center; }
    #controls {
      position: fixed; right: 20px; top: 20px;
      background: rgba(250, 249, 245, 0.95);
      padding: 16px; border-radius: 8px;
      font-family: -apple-system, sans-serif;
    }
    .control-group { margin-bottom: 12px; }
    label { display: block; font-size: 12px; color: #141413; }
    input[type="range"] { width: 150px; }
  </style>
</head>
<body>
  <div id="canvas-container"></div>
  <div id="controls">
    <div class="control-group">
      <label>Seed: <span id="seed-val">12345</span></label>
      <input type="range" id="seed-slider" min="1" max="99999" value="12345">
    </div>
    <div class="control-group">
      <label>Particle Count: <span id="count-val">500</span></label>
      <input type="range" id="count-slider" min="100" max="2000" value="500">
    </div>
    <button id="regenerate">Regenerate</button>
  </div>
  <script>
    let seed = 12345;
    let particleCount = 500;
    let particles = [];

    function setup() {
      const canvas = createCanvas(800, 800);
      canvas.parent('canvas-container');
      randomSeed(seed);
      noiseSeed(seed);
      initParticles();
      noLoop();
    }

    function initParticles() {
      particles = [];
      for (let i = 0; i < particleCount; i++) {
        particles.push({
          x: random(width),
          y: random(height),
          vx: 0,
          vy: 0
        });
      }
    }

    function draw() {
      background(20, 20, 19);
      particles.forEach(p => {
        const angle = noise(p.x * 0.005, p.y * 0.005) * TWO_PI * 4;
        p.vx = cos(angle) * 2;
        p.vy = sin(angle) * 2;
        p.x += p.vx;
        p.y += p.vy;
        stroke(217, 119, 87, 150);
        point(p.x, p.y);
      });
    }

    document.getElementById('seed-slider').addEventListener('input', e => {
      seed = parseInt(e.target.value);
      document.getElementById('seed-val').textContent = seed;
      randomSeed(seed);
      noiseSeed(seed);
      initParticles();
      redraw();
    });
    document.getElementById('regenerate').addEventListener('click', () => {
      seed = floor(random(99999));
      document.getElementById('seed-slider').value = seed;
      document.getElementById('seed-val').textContent = seed;
      randomSeed(seed);
      noiseSeed(seed);
      initParticles();
      redraw();
    });
  </script>
</body>
</html>
```

## 7. 前端组件设计

来自 frontend-design。

### 7.1 组件粒度原则

- **单一职责**：一个组件做一件事。按钮只做按钮，不做数据获取
- **组合优于配置**：用 `<Card><Card.Header><Card.Body>` 而非 `<Card variant="with-header-and-body">`
- **状态上提**：共享状态上提到最近公共父级，不要全局 store 满天飞

### 7.2 命名：从用户视角

```javascript
// ❌ 错误：从系统视角命名
<WebhookConfigForm />
<ApiResponseRenderer />

// ✅ 正确：从用户视角命名
<NotificationSettings />
<DataPreview />
```

### 7.3 状态管理决策树

```
状态被几个组件共享？
├─ 1 个 → 组件内 useState
├─ 2-3 个邻近组件 → 上提到父级 useState
├─ 跨页面 / 跨路由 → 全局 store（Zustand / Redux / Pinia）
└─ 跨会话持久 → localStorage + useState
```

### 7.4 可访问性（a11y）必做项

- 所有交互元素键盘可达（`tabindex` / 语义化 button/a）
- 焦点可见（`:focus-visible` 样式不丢）
- 图片有 `alt`（装饰性图片用 `alt=""`）
- 颜色对比度 ≥ 4.5:1（用 WebAIM Contrast Checker 验证）
- 表单字段有 `<label>` 关联
- 暗模式对比度也要达标

```css
/* 焦点可见 */
*:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: 2px;
}

/* reduced motion 尊重 */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
```

### 7.5 文案是设计材料

来自 frontend-design：
> 词出现在设计中只有一个原因：让它更易理解、更易用。词是设计材料，不是装饰。

- **动词主动**：按钮写"保存修改"而非"提交"；写"发布"而非"提交表单"
- **同操作同名**：按钮"发布" → toast"已发布"，不要按钮"发布" → toast"创建成功"
- **错不道歉**：写"网络错误，重试"而非"对不起，出错了"
- **空状态是指令**：空列表写"暂无项目，点右上角'新建'开始"而非"暂无数据"

## 8. 主题系统生成

来自 theme-factory。

### 8.1 CSS 变量 + 设计令牌

```css
:root {
  /* === Color Tokens === */
  --color-primary: #2e7d32;
  --color-primary-hover: #1b5e20;
  --color-bg: #faf9f5;
  --color-text: #141413;
  --color-border: #e8e6dc;

  /* === Spacing Tokens === */
  --space-1: 4px;
  --space-4: 16px;
  --space-8: 32px;

  /* === Radius Tokens === */
  --radius-sm: 2px;
  --radius-md: 4px;
  --radius-lg: 8px;

  /* === Typography Tokens === */
  --font-display: "Poppins", sans-serif;
  --font-body: "Lora", serif;
  --text-base: 1rem;
  --text-2xl: 1.5rem;

  /* === Shadow Tokens === */
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.05);
}

/* 暗模式覆盖 */
@media (prefers-color-scheme: dark) {
  :root {
    --color-bg: #141413;
    --color-text: #faf9f5;
    --color-border: #2a2a28;
    --color-primary: #66bb6a;  /* 暗模式主色提亮 */
  }
}

/* 手动切换主题 */
[data-theme="dark"] {
  --color-bg: #141413;
  --color-text: #faf9f5;
}
```

### 8.2 10 个预设主题参考

来自 theme-factory 的预设主题，可作创意起点：

| 主题名 | 主色 | 副色 | 字体风格 |
|--------|------|------|---------|
| Ocean Depths | 深海蓝 #1e3a5f | 浅蓝 #6a9bcc | 现代 Sans |
| Sunset Boulevard | 暖橙 #d97757 | 紫 #8b5cf6 | 衬线 Display |
| Forest Canopy | 森林绿 #2e7d32 | 苔藓 #788c5d | 自然 Sans |
| Modern Minimalist | 灰 #4a4a4a | 浅灰 #b0aea5 | 几何 Sans |
| Golden Hour | 金 #d4a574 | 棕 #8b6f47 | 复古 Serif |
| Arctic Frost | 冰蓝 #6a9bcc | 白 #f0f4f8 | 清爽 Sans |
| Desert Rose | 玫瑰粉 #c08497 | 沙色 #d4a574 | 优雅 Serif |
| Tech Innovation | 电光蓝 #0ea5e9 | 紫 #8b5cf6 | Mono |
| Botanical Garden | 草绿 #788c5d | 黄 #d4a574 | 手写感 |
| Midnight Galaxy | 深紫 #1e1b4b | 金 #fbbf24 | 衬线 Display |

### 8.3 主题切换 JS

```javascript
function setTheme(themeName) {
  document.documentElement.setAttribute('data-theme', themeName);
  localStorage.setItem('theme', themeName);
}

// 初始化
const savedTheme = localStorage.getItem('theme') || 'auto';
if (savedTheme === 'auto') {
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.setAttribute('data-theme', prefersDark ? 'dark' : 'light');
} else {
  setTheme(savedTheme);
}
```

## 9. Web Artifact 工作流

来自 web-artifacts-builder。

### 9.1 从需求到单文件 HTML 的 5 步流程

```
1. 需求捕获 → 2. 设计 plan → 3. 开发 → 4. 打包 → 5. 验证
```

**Step 1：需求捕获**
- 用户要什么？（Dashboard / 工具页 / 报告 / 游戏）
- 单页还是多页？
- 需要交互吗？还是纯展示？
- 数据来源：静态 / API / 用户输入

**Step 2：设计 plan**（来自 frontend-design 的两阶段）
- 写一份 compact token system：4-6 个颜色 hex + 2+ 字体角色 + 布局概念 + signature 元素
- 自检：plan 是否会"用给任何相似 brief 都一样"？是 → 重新设计

**Step 3：开发**
- 单文件：直接写 HTML+CSS+JS 内联
- 多文件：用 Vite 开发，保持源码结构

**Step 4：打包**
- 单文件：直接交付 `.html`
- 多文件：`parcel build index.html && html-inline dist/index.html bundle.html`

**Step 5：验证**
- 截图 + OCR 看是否达到设计预期（本项目用 `screen_ocr` / `screen_analyze`）
- 跨浏览器测试（Chrome / Firefox / Safari）
- 响应式测试（375px / 768px / 1280px / 1920px）

### 9.2 何时不用单文件 HTML

- 团队协作（多文件 VCS 友好）
- 大型应用（单文件 > 1MB 时浏览器解析慢）
- 需要 SSR / SEO（单文件客户端渲染 SEO 差）
- 需要代码分割 / 懒加载

### 9.3 测试时机

来自 web-artifacts-builder：
> 测试是**可选**步骤，只有在必要时执行。一开始就测试会增加请求和成品之间的延迟。**先交付成品，必要时再测试**。

本项目推荐：用 `webapp_testing.md` 的 Reconnaissance-Then-Action 模式测试，先 `networkidle` 再截图。

## 10. 用户偏好

来自 LocalAgent 项目用户偏好（实测）。

### 10.1 必须遵守的偏好

| 偏好 | 说明 | 实现方式 |
|------|------|----------|
| **绿色主色** | 用户偏好自然系绿色，不要紫色 | `--color-primary: #2e7d32;` 系列 |
| **避免紫色** | 紫色被 AI 滥用，用户反感 | 主题色板里禁用紫蓝 / 紫粉 |
| **PySide6 优于 web** | 桌面应用首选 PySide6，不做 Web 版 | 项目内 `client/` 用 PySide6，工具页才用 web |
| **未读数指示** | 列表 / 标签要有未读计数徽章 | `<span class="badge">5</span>` |
| **按钮宽度** | 主按钮宽度固定，不要撑满 | `width: 120px;` 或 `min-width: 80px;` |
| **只读状态** | 不可操作元素有视觉提示 | `opacity: 0.5; cursor: not-allowed;` |

### 10.2 次要偏好

- 表格用斑马纹（`tbody tr:nth-child(even) { background: var(--color-bg-alt); }`）
- 主操作按钮在右下，取消按钮在左下
- 弹窗用模态遮罩 + ESC 关闭
- 长列表分页或虚拟滚动，不要一次渲染 1000+
- 图标用 Lucide / Heroicons，不用 emoji
- 中文字体优先 PingFang SC / Source Han Sans

### 10.3 反感清单（不要做）

- 紫色 / 紫蓝 / 紫粉渐变
- 过度动画（每次点击都弹跳）
- 模糊背景（backdrop-filter: blur）滥用
- 满屏 emoji 图标
- 单一 Inter 字体
- 所有元素统一 12px 圆角
- 全居中布局
- AI 默认的"暖米色 + 赤陶 + 衬线"组合（除非 brief 明确要这个 look）

## 11. 反模式汇总

| # | 反模式 | 来源 skill | 纠正 |
|---|--------|-----------|------|
| 1 | 用 Inter 单字族 | frontend-design | 配 display + body 两个字族 |
| 2 | 紫色渐变 | frontend-design | 选与主题相关的具象色 |
| 3 | 全 8px 圆角 | frontend-design | 区分图/卡片/按钮 |
| 4 | 全居中 | frontend-design | 用列宽 + 留白制造节奏 |
| 5 | emoji 当图标 | 本项目 | 用 SVG icon |
| 6 | 不处理 HiDPI | canvas-design | dpr 缩放 |
| 7 | 不用 seed | algorithmic-art | randomSeed/noiseSeed 保证可复现 |
| 8 | 单一大组件 | frontend-design | 组合优于配置 |
| 9 | 系统视角命名 | frontend-design | 从用户视角命名 |
| 10 | 默认暖米色 + 赤陶 | frontend-design | brief 没指明时跳出默认 |
| 11 | 提交表单后 toast "创建成功" | frontend-design | 同操作同名：toast "已发布" |
| 12 | 错误信息"对不起" | frontend-design | 不道歉，写"网络错误，重试" |
| 13 | 空列表只写"暂无数据" | frontend-design | 空状态是指令："点新建开始" |
| 14 | 不写 alt | a11y | 装饰性 alt="" / 信息性具体描述 |
| 15 | focus 样式丢 | a11y | `:focus-visible` 必加 |

## 12. 来源文件参考

- anthropics/skills 原始 SKILL.md：
  - `https://github.com/anthropics/skills/blob/main/skills/algorithmic-art/SKILL.md`
  - `https://github.com/anthropics/skills/blob/main/skills/brand-guidelines/SKILL.md`
  - `https://github.com/anthropics/skills/blob/main/skills/canvas-design/SKILL.md`
  - `https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md`
  - `https://github.com/anthropics/skills/blob/main/skills/web-artifacts-builder/SKILL.md`
  - `https://github.com/anthropics/skills/blob/main/skills/theme-factory/SKILL.md`
- 本项目 html-dev-debug skill（已强化，含 Reconnaissance-Then-Action + anti-AI-slop 设计规范）：`<project_root>\.agents\skills\html-dev-debug.md`
- 本项目 webapp_testing 工具包：`workspace/dev_toolkit/webapp_testing.md`
