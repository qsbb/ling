"""出站节奏与串行：段间最小间隔、每窗口令牌桶、同窗口不许交错。

依据 `docs/OUTBOUND-DELIVERY-RESEARCH.md` §5 P1-2：AstrBot 的 1.5–3.5 秒
随机停顿是**拟人节奏**，MaiBot 用"打字时长"，两家都没有真正的令牌桶；
NapCat 侧也没有频控。ling 需要的是"别刷屏 + 别交错"，所以：

1. 段间最小间隔 + 抖动（不是固定值，否则节奏太机械）；
2. 每窗口令牌桶（突发 3 段，之后每 3 秒补 1 段）；
3. 同一窗口的发送**串行**（两个并发轮次不能把话交错着说）。

这里用假时钟/假 sleep 验证**调度决策**，不依赖真实时间（否则测试会被
真实等待拖成分钟级）。
"""

from __future__ import annotations

import asyncio

import pytest

import ling.kernel.bridge as bridge_module


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def paced(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """把"基础间隔"打开、把真实 sleep 换成"推进假时钟"。"""
    import ling.capabilities.chunking as chunking

    clock = _FakeClock()
    monkeypatch.setattr(bridge_module, "time", clock)
    monkeypatch.setattr(chunking, "PART_DELAY_SECONDS", 1.2)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(bridge_module, "_sleep", fake_sleep)
    return clock, slept


@pytest.mark.asyncio
async def test_first_part_is_not_delayed(paced) -> None:  # noqa: ANN001
    """第 1 段之前不许有节奏性停顿（AstrBot 那样首条凭空晚 1.5–3.5 秒）。"""
    _clock, slept = paced
    gate = bridge_module.WindowSendGate()
    async with gate.lock:
        await gate.pace(first=True)
    assert slept == []


@pytest.mark.asyncio
async def test_later_parts_wait_in_the_jitter_window(paced) -> None:  # noqa: ANN001
    _clock, slept = paced
    gate = bridge_module.WindowSendGate()
    low, high = bridge_module.PART_INTERVAL_JITTER
    async with gate.lock:
        await gate.pace(first=True)
        await gate.pace(first=False)
    assert len(slept) == 1
    assert 1.2 * low <= slept[0] <= 1.2 * high


@pytest.mark.asyncio
async def test_token_bucket_limits_burst(paced) -> None:  # noqa: ANN001
    """突发容量用完之后，每多一段都要等桶补上一个令牌。"""
    _clock, slept = paced
    gate = bridge_module.WindowSendGate()
    capacity = int(bridge_module.SEND_BUCKET_CAPACITY)
    parts = capacity + 2  # 桶空之后还要再发两段，第二段必须等补令牌
    async with gate.lock:
        for index in range(parts):
            await gate.pace(first=index == 0)
    # 抖动本身会给"除第 1 段外每段"一次睡眠；桶空之后会**多**一次等待，
    # 所以睡眠次数必然超过纯抖动的 parts-1 次。
    assert len(slept) > parts - 1, slept


@pytest.mark.asyncio
async def test_disabled_pacing_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PART_DELAY_SECONDS = 0` = 关掉节奏与令牌桶（测试用）。"""
    import ling.capabilities.chunking as chunking

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(chunking, "PART_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(bridge_module, "_sleep", fake_sleep)
    gate = bridge_module.WindowSendGate()
    async with gate.lock:
        for index in range(6):
            await gate.pace(first=index == 0)
    assert slept == []


@pytest.mark.asyncio
async def test_same_window_sends_are_serialized(paced) -> None:  # noqa: ANN001
    """同一窗口的两个并发发送不能交错——用户不该看到 A1 B1 A2 B2。"""
    _clock, _slept = paced
    bridge_module.reset_send_gates()
    gate = bridge_module._gate_for("inst:private:100200300")
    order: list[str] = []

    async def sender(name: str) -> None:
        async with gate.lock:
            await gate.pace(first=False)
            order.append(f"{name}-start")
            await asyncio.sleep(0)  # 让出控制权：没有锁的话另一段会插进来
            order.append(f"{name}-end")

    await asyncio.gather(sender("A"), sender("B"))
    # 每一对 start/end 必须相邻：A-start, A-end, B-start, B-end（或 B 在前）
    assert order in (
        ["A-start", "A-end", "B-start", "B-end"],
        ["B-start", "B-end", "A-start", "A-end"],
    ), order


@pytest.mark.asyncio
async def test_different_windows_do_not_block_each_other(paced) -> None:  # noqa: ANN001
    """闸门是**每窗口**的：给 A 窗口限速不该拖住 B 窗口。"""
    _clock, _slept = paced
    bridge_module.reset_send_gates()
    gate_a = bridge_module._gate_for("inst:private:A")
    gate_b = bridge_module._gate_for("inst:private:B")
    assert gate_a is not gate_b
    order: list[str] = []

    async def sender(gate, name: str) -> None:  # noqa: ANN001
        async with gate.lock:
            await gate.pace(first=True)
            order.append(f"{name}-start")
            await asyncio.sleep(0)
            order.append(f"{name}-end")

    await asyncio.gather(sender(gate_a, "A"), sender(gate_b, "B"))
    # 不同窗口可以交错（没有跨窗口锁）
    assert order[0] != order[-1], order
