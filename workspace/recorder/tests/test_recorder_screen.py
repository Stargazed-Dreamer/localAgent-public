"""测试 ScreenCaptureSensor：帧哈希去重 + 多帧采样 + 三种截图模式

策略（D023 / D024 / D035）：
- ClickBurstDedup：纯函数测试，3 帧一样/3 帧不同/部分相同/边界
- pHash（D035）：compute_phash / phash_distance 纯函数 + ScreenCaptureSensor 去重行为
- ScreenCaptureSensor：注入 fake capture_fn 返回 PIL Image，避免 mock mss/PrintWindow
- 集成：注册到 RecordingController，验证 frames/ 写 PNG + events.jsonl 上报 screen_frame

为什么用 capture_fn 注入而不是 monkeypatch mss：
- mss / PrintWindow 是底层系统 API，mock 复杂且脆弱
- capture_fn 是 sensor 的天然抽象点（"返回一张当前屏幕/窗口的 PIL Image 或 None"）
- 真实 mss/PrintWindow 实现作为默认 capture_fn 在 sensor 内部构造，不测（需真实桌面）
"""

import time

import numpy as np
from PIL import Image

from lib.recorder.controller import RecordingController
from lib.recorder.sensors.screen import (
    ClickBurstDedup,
    ScreenCaptureSensor,
    compute_phash,
    phash_distance,
)


def make_image(color=(255, 0, 0), size=(10, 10)) -> Image.Image:
    """生成一张纯色测试图。"""
    return Image.new("RGB", size, color)


# ========== ClickBurstDedup 单元测试（spec D023） ==========

class TestClickBurstDedup:
    """spec D023：3 帧一样留 1 张，有变化全留。"""

    def test_all_same_returns_one(self):
        """3 帧哈希完全相同 → 只留第 1 张。"""
        a = b"\x00" * 100
        result = ClickBurstDedup.dedup([a, a, a])
        assert len(result) == 1
        assert result[0] == a

    def test_all_different_returns_all(self):
        """3 帧全不同 → 全部保留。"""
        a, b, c = b"\x00" * 100, b"\x01" * 100, b"\x02" * 100
        result = ClickBurstDedup.dedup([a, b, c])
        assert len(result) == 3
        assert result == [a, b, c]

    def test_partial_same_returns_all(self):
        """部分相同 → 全部保留。

        spec："有变化全留"，目的捕捉瞬态变化（如复选框切换后又变回）。
        - [A,A,B] 有变化 → 全留
        - [A,B,B] 有变化 → 全留
        - [A,B,A] 复选框切换后又变回 → 全留
        """
        a, b = b"\x00" * 100, b"\x01" * 100
        assert len(ClickBurstDedup.dedup([a, a, b])) == 3
        assert len(ClickBurstDedup.dedup([a, b, b])) == 3
        assert len(ClickBurstDedup.dedup([a, b, a])) == 3

    def test_empty_returns_empty(self):
        """空列表 → 空列表。"""
        assert ClickBurstDedup.dedup([]) == []

    def test_single_returns_single(self):
        """单帧 → 单帧。"""
        a = b"\x00" * 100
        assert ClickBurstDedup.dedup([a]) == [a]

    def test_two_same_returns_one(self):
        """2 帧相同 → 留 1 张（"全相同才去重"逻辑对任意长度适用）。"""
        a = b"\x00" * 100
        assert len(ClickBurstDedup.dedup([a, a])) == 1

    def test_two_different_returns_both(self):
        """2 帧不同 → 全留。"""
        a, b = b"\x00" * 100, b"\x01" * 100
        assert len(ClickBurstDedup.dedup([a, b])) == 2


# ========== ScreenCaptureSensor 生命周期 ==========

class TestScreenCaptureSensorLifecycle:
    def test_start_stop_idempotent(self, tmp_path):
        """start + stop 幂等，重复调用不崩溃。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = ScreenCaptureSensor(
            controller=controller,
            interval=0.05,
            capture_fn=lambda: make_image(),
        )
        sensor.start()
        time.sleep(0.1)
        sensor.stop()
        sensor.stop()  # 幂等
        controller.stop()

    def test_stop_without_start_no_crash(self, tmp_path):
        """未 start 直接 stop 不崩溃。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        sensor = ScreenCaptureSensor(
            controller=controller,
            capture_fn=lambda: make_image(),
        )
        sensor.stop()  # 不应抛异常


# ========== 集成测试 ==========

class TestScreenCaptureIntegration:
    def test_periodic_capture_writes_png(self, tmp_path):
        """定时截图 → frames/ 下有 PNG 文件可被 PIL 打开。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = ScreenCaptureSensor(
            controller=controller,
            interval=0.05,
            capture_fn=lambda: make_image((255, 0, 0)),
        )
        sensor.start()
        time.sleep(0.2)
        sensor.stop()
        controller.stop()

        pngs = list(controller.package.frames_dir.glob("*.png"))
        assert len(pngs) > 0
        img = Image.open(pngs[0])
        assert img.format == "PNG"

    def test_emits_screen_frame_event(self, tmp_path):
        """截图后上报 screen_frame 事件到 events.jsonl。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = ScreenCaptureSensor(
            controller=controller,
            interval=0.05,
            capture_fn=lambda: make_image(),
        )
        sensor.start()
        time.sleep(0.2)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        screen_events = [e for e in events if e["kind"] == "screen_frame"]
        assert len(screen_events) > 0
        payload = screen_events[0]["payload"]
        assert "frame_path" in payload
        assert "frame_seq" in payload

    def test_capture_returns_none_skips(self, tmp_path):
        """capture_fn 返回 None（窗口最小化）→ 跳过本次截图不发事件不写盘。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = ScreenCaptureSensor(
            controller=controller,
            interval=0.05,
            capture_fn=lambda: None,  # 模拟窗口最小化/截图失败
        )
        sensor.start()
        time.sleep(0.2)
        sensor.stop()
        controller.stop()

        events = controller.package.read_events()
        screen_events = [e for e in events if e["kind"] == "screen_frame"]
        assert len(screen_events) == 0
        pngs = list(controller.package.frames_dir.glob("*.png"))
        assert len(pngs) == 0

    def test_click_burst_3_same_returns_1(self, tmp_path):
        """click 触发多帧采样：3 帧一样只留 1 张（D023）。

        用 capture_burst() 同步执行避免定时线程干扰。
        """
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        same_img = make_image((0, 0, 255))
        sensor = ScreenCaptureSensor(
            controller=controller,
            burst_intervals=(0.01, 0.02, 0.03),
            capture_fn=lambda: same_img,
        )
        sensor.capture_burst()  # 同步执行，不起定时线程
        controller.stop()

        pngs = list(controller.package.frames_dir.glob("*.png"))
        assert len(pngs) == 1, f"3 帧一样应只留 1 张，实际 {len(pngs)} 张"

    def test_click_burst_3_different_returns_3(self, tmp_path):
        """click 触发多帧采样：3 帧不同全留（D023）。

        用 capture_burst() 同步执行避免定时线程干扰。
        """
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        images = [
            make_image((255, 0, 0)),
            make_image((0, 255, 0)),
            make_image((0, 0, 255)),
        ]
        idx = [0]

        def fake_capture():
            i = idx[0]
            idx[0] = min(i + 1, len(images) - 1)
            return images[i]

        sensor = ScreenCaptureSensor(
            controller=controller,
            burst_intervals=(0.01, 0.02, 0.03),
            capture_fn=fake_capture,
        )
        sensor.capture_burst()  # 同步执行，不起定时线程
        controller.stop()

        pngs = list(controller.package.frames_dir.glob("*.png"))
        assert len(pngs) == 3, f"3 帧不同应全留，实际 {len(pngs)} 张"

    def test_batch_write_buffered_and_flush_on_stop(self, tmp_path):
        """缓冲批量写：截图数 < batch_size 时 stop 触发 flush。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = ScreenCaptureSensor(
            controller=controller,
            interval=0.02,
            batch_size=100,  # 大 batch，确保定时截图不触发 flush
            capture_fn=lambda: make_image(),
        )
        sensor.start()
        time.sleep(0.1)  # 约 5 帧定时截图
        sensor.stop()  # stop 必须 flush 剩余缓冲
        controller.stop()

        pngs = list(controller.package.frames_dir.glob("*.png"))
        assert len(pngs) > 0, "stop 后缓冲应已 flush"

    def test_frame_count_incremented_in_controller(self, tmp_path):
        """截图后调 controller.increment_frame_count，meta.json 的 frame_count 正确。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = ScreenCaptureSensor(
            controller=controller,
            interval=0.05,
            capture_fn=lambda: make_image(),
        )
        sensor.start()
        time.sleep(0.2)
        sensor.stop()
        controller.stop()

        assert controller.frame_count > 0
        meta = controller.package.read_meta()
        assert meta["frame_count"] == controller.frame_count


# ========== pHash 纯函数测试（D035） ==========


class TestComputePhash:
    """spec D035：compute_phash 计算 8×8 灰度均值哈希。"""

    def test_same_image_same_hash(self):
        """相同图片 → 相同 hash。"""
        img = make_image((128, 128, 128), size=(32, 32))
        h1 = compute_phash(img)
        h2 = compute_phash(img)
        assert h1 == h2

    def test_pure_color_hash_is_zero_or_all_ones(self):
        """纯色图：所有像素 = 均值，arr >= mean 全 True → 0xFFFFFFFFFFFFFFFF。

        注：numpy 的 >= 含等号，纯色图所有像素 = mean，故所有 bit = 1。
        """
        img = make_image((100, 100, 100), size=(8, 8))
        h = compute_phash(img)
        assert h == 0xFFFFFFFFFFFFFFFF, f"纯色图应为全 1，实际 {hex(h)}"

    def test_half_black_half_white_distinct_from_pure(self):
        """上半黑下半白 → 与纯黑/纯白 hash 不同。"""
        arr = np.zeros((8, 8, 3), dtype=np.uint8)
        arr[:4, :] = 0      # 上半黑
        arr[4:, :] = 255    # 下半白
        img = Image.fromarray(arr, "RGB")
        h_half = compute_phash(img)
        h_black = compute_phash(make_image((0, 0, 0), size=(8, 8)))
        h_white = compute_phash(make_image((255, 255, 255), size=(8, 8)))
        assert h_half != h_black
        assert h_half != h_white

    def test_hash_is_64bit(self):
        """hash 值在 64-bit 范围内。"""
        img = make_image((50, 100, 150), size=(20, 20))
        h = compute_phash(img)
        assert 0 <= h <= 0xFFFFFFFFFFFFFFFF

    def test_different_images_different_hash(self):
        """明显不同的两张图 hash 不同（汉明距离较大）。

        用随机噪声图（非纯色），确保 hash 有显著差异。
        纯色图所有像素 = 均值，hash 都是 0xFFFFFFFFFFFFFFFF，无法区分。
        """
        rng_a = np.random.RandomState(42)
        arr_a = rng_a.randint(0, 256, size=(32, 32, 3), dtype=np.uint8)
        img_a = Image.fromarray(arr_a, "RGB")

        rng_b = np.random.RandomState(123)
        arr_b = rng_b.randint(0, 256, size=(32, 32, 3), dtype=np.uint8)
        img_b = Image.fromarray(arr_b, "RGB")

        h_a = compute_phash(img_a)
        h_b = compute_phash(img_b)
        assert h_a != h_b
        assert phash_distance(h_a, h_b) > 5, "随机噪声图汉明距离应显著大于阈值"


class TestPhashDistance:
    """spec D035：phash_distance 计算汉明距离。"""

    def test_identical_hashes_distance_zero(self):
        """相同 hash → 距离 0。"""
        h = 0xDEADBEEF
        assert phash_distance(h, h) == 0

    def test_complement_hashes_distance_64(self):
        """互补 hash（全 0 vs 全 1）→ 距离 64。"""
        assert phash_distance(0, 0xFFFFFFFFFFFFFFFF) == 64

    def test_one_bit_difference_distance_one(self):
        """仅 1 bit 不同 → 距离 1。"""
        h1 = 0b0001
        h2 = 0b0011
        assert phash_distance(h1, h2) == 1

    def test_distance_is_symmetric(self):
        """汉明距离对称：d(a,b) == d(b,a)。"""
        a, b = 0x123456789ABCDEF0, 0x0FEDCBA987654321
        assert phash_distance(a, b) == phash_distance(b, a)

    def test_known_distance(self):
        """已知例：0xFF vs 0xF0 → 4 bit 不同。"""
        assert phash_distance(0xFF, 0xF0) == 4


# ========== ScreenCaptureSensor pHash 去重行为测试（D035） ==========


class TestScreenCapturePhashDedup:
    """spec D035：ScreenCaptureSensor 定时截图 pHash 相邻帧去重。"""

    def test_disabled_threshold_no_dedup(self, tmp_path):
        """phash_threshold=0 → 禁用去重，所有帧都写盘。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        # 用同一张图，threshold=0 应保留所有帧
        sensor = ScreenCaptureSensor(
            controller=controller,
            interval=0.02,
            batch_size=100,
            capture_fn=lambda: make_image((100, 100, 100)),
            phash_threshold=0,
        )
        sensor.start()
        time.sleep(0.15)  # 约 7 帧
        sensor.stop()
        controller.stop()

        pngs = list(controller.package.frames_dir.glob("*.png"))
        assert len(pngs) >= 5, f"threshold=0 应保留所有帧，实际 {len(pngs)} 张"
        assert sensor._phash_skipped_count == 0

    def test_same_image_dedup_to_one(self, tmp_path):
        """phash_threshold>0 + 完全相同图 → 仅第 1 张写盘，其余跳过。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = ScreenCaptureSensor(
            controller=controller,
            interval=0.02,
            batch_size=100,
            capture_fn=lambda: make_image((100, 100, 100)),
            phash_threshold=5,
        )
        sensor.start()
        time.sleep(0.15)  # 约 7 帧，第 1 张写盘，其余 6 张跳过
        sensor.stop()
        controller.stop()

        pngs = list(controller.package.frames_dir.glob("*.png"))
        assert len(pngs) == 1, f"完全相同图应只留 1 张，实际 {len(pngs)} 张"
        assert sensor._phash_skipped_count >= 5

    def test_clearly_different_images_all_kept(self, tmp_path):
        """phash_threshold=5 + 每帧 hash 显著不同 → 全部保留。

        用随机噪声图（非纯色），每张 hash 差异大（汉明距离 ~32）。
        纯色图 hash 都是 0xFFFFFFFFFFFFFFFF，会被去重到 1 张。
        """
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        # 7 张随机噪声图，每张 hash 差异大
        images = []
        for seed in range(7):
            rng = np.random.RandomState(seed)
            arr = rng.randint(0, 256, size=(32, 32, 3), dtype=np.uint8)
            images.append(Image.fromarray(arr, "RGB"))

        idx = [0]

        def fake_capture():
            i = idx[0]
            idx[0] = min(i + 1, len(images) - 1)
            return images[i]

        sensor = ScreenCaptureSensor(
            controller=controller,
            interval=0.02,
            batch_size=100,
            capture_fn=fake_capture,
            phash_threshold=5,
        )
        sensor.start()
        time.sleep(0.18)  # 约 8 帧
        sensor.stop()
        controller.stop()

        pngs = list(controller.package.frames_dir.glob("*.png"))
        # 至少 5 张保留（随机噪声 hash 差异大，汉明距离 >> 5）
        assert len(pngs) >= 5, f"显著不同图应保留多张，实际 {len(pngs)} 张"

    def test_start_resets_phash_state(self, tmp_path):
        """start() 重置 _last_phash 和 _phash_skipped_count（新录制会话不继承旧 hash）。

        第二轮用返回 None 的 capture_fn，确保线程不更新 _last_phash，
        从而能同步观测到 start() 的重置效果（避免线程竞态）。
        """
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        sensor = ScreenCaptureSensor(
            controller=controller,
            interval=0.02,
            batch_size=100,
            capture_fn=lambda: make_image((100, 100, 100)),
            phash_threshold=5,
        )
        sensor.start()
        time.sleep(0.08)
        sensor.stop()
        assert sensor._phash_skipped_count > 0, "第一轮应有跳过帧"
        assert sensor._last_phash is not None, "第一轮 _last_phash 应已设置"

        # 第二次 start：用返回 None 的 capture_fn，确保线程不更新 _last_phash
        sensor._capture_fn = lambda: None
        sensor.start()
        # _phash_skipped_count 应被重置为 0（同步发生在 start() 中，线程启动前）
        assert sensor._phash_skipped_count == 0, "start() 应重置 _phash_skipped_count"
        # _last_phash 应被重置为 None（capture_fn 返回 None，线程不更新它）
        assert sensor._last_phash is None, "start() 应重置 _last_phash"
        sensor.stop()
        controller.stop()

    def test_phash_does_not_affect_burst(self, tmp_path):
        """phash_threshold 仅作用于定时截图，click burst 路径不受影响。"""
        controller = RecordingController(base_dir=tmp_path, sensors=[])
        controller.start()
        # burst 用相同图，ClickBurstDedup 会把 3 张相同 dedup 到 1 张
        # phash_threshold 不应介入 burst 路径
        sensor = ScreenCaptureSensor(
            controller=controller,
            burst_intervals=(0.01, 0.02, 0.03),
            capture_fn=lambda: make_image((100, 100, 100)),
            phash_threshold=5,
        )
        sensor.capture_burst()  # 同步执行 burst
        controller.stop()

        pngs = list(controller.package.frames_dir.glob("*.png"))
        # burst 3 帧相同 → ClickBurstDedup 保留 1 张（与 phash 无关）
        assert len(pngs) == 1, f"burst 路径应只受 ClickBurstDedup 影响，实际 {len(pngs)} 张"
        # burst 不更新 _last_phash（burst 后 _last_phash 仍为 None）
        assert sensor._last_phash is None, "burst 路径不应更新 _last_phash"
