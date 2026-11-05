"""B4 登录加固：计时侧信道 + 不阻塞事件循环 + 失败限速。

## 这组用例防的是什么

原来的 `login()` 是一行短路求值：

```python
if user is None or user.disabled or not verify_password(password, user.password_hash):
```

三个缺陷同时藏在里面：

1. **计时侧信道**：用户不存在时 scrypt 根本不跑，响应快一个数量级
   （实测 ~16ms vs 亚毫秒）。错误体一模一样，所以"这个账号存在吗"
   的答案从文案挪到了**时间轴**上——照样能枚举账号。
2. **阻塞事件循环**：scrypt 是同步 C 调用，单 worker 部署下直接在事件
   循环里跑，并发 N 次登录 ≈ N×16ms 的全站停顿。
3. **无限速**：失败只记日志，不累计不阻断。

## 为什么用 monkeypatch 控制 scrypt 耗时

真实 scrypt 一次约 16-20ms，靠 `perf_counter` 比较两条路径在 CI 上会被
调度抖动吃掉。所以这里把 `verify_password` 换成**固定 5ms 的假实现**，
断言"两条路径都调用了它"以及"两条路径的总耗时都在同一个量级"。
这样测的是**调用结构**（短路有没有跳过 scrypt），而不是 CPU 快慢——
后者本来就该由机器的负载决定。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from ling.domains.identity.service import LoginRateLimiter
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

FAKE_SCRYPT_SECONDS = 0.060
"""假 scrypt 的固定耗时。

为什么取 60ms（真实 scrypt 约 16-20ms）：这台机器上同一路径重复请求的
实测耗时在 **60-190ms** 之间乱跳（asyncpg 连接池、日志、GC、其他并行
子智能体在跑测试都在掺和）。若假 scrypt 取真实量级的 16ms，"跳过 scrypt"
这个信号就完全被噪声盖住，用例只能靠运气通过。

取 60ms 之后，用**最小样本 + 丢弃首个请求**（噪声只加不减，所以 min
最接近路径本身的成本）就能稳定分辨：实测两条路径的 min 都在 66-75ms，
而跳过 scrypt 的路径会掉到 10ms 以下。代价是这条用例慢约 1 秒。
"""


@pytest.fixture
def fake_scrypt(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """把 scrypt 换成固定耗时 + 记录调用参数。

    返回值是调用记录 `[(password, stored_hash), ...]`，用例据此断言
    "用户不存在"那条路径**也**走到了校验。
    """
    calls: list[tuple[str, str]] = []

    def _fake_verify(password: str, stored: str) -> bool:
        calls.append((password, stored))
        time.sleep(FAKE_SCRYPT_SECONDS)
        # 只有真口令才算对；dummy 哈希永远不匹配（与真实实现语义一致）
        return password == "tester-password" and not stored.startswith("scrypt$16384$8$1$bGluZy1kdW1teQ")

    import ling.domains.identity.service as svc

    monkeypatch.setattr(svc, "verify_password", _fake_verify)
    return calls


@pytest.fixture
def limiter(web_env: TestEnv) -> LoginRateLimiter:
    """每个用例一个干净的计数器（app.state 上是跨用例共享的）。

    阈值调到很高：这组用例大多要重复登录十来次，默认的 5 次阈值会让
    "计时"和"并发"这两条用例被限速拦在前面，测到的就不是 scrypt 了。
    专门测限速的用例自己调回真实阈值。
    """
    lim = LoginRateLimiter()
    lim.FAILURE_THRESHOLD = 1000
    lim.IP_FAILURE_THRESHOLD = 1000
    web_env.app.state.login_limiter = lim  # type: ignore[attr-defined]
    return lim


async def _login(env: TestEnv, username: str, password: str):
    return await env.client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )


def _error_shape(response) -> dict[str, object]:
    """错误体去掉 `trace_id` 后的形状。

    `trace_id` 是**每次请求**的关联 ID，本来就该不同（它是排查用的，
    不是身份信息）；比较"是否泄露了失败原因"要把它排除掉。
    """
    error = dict(response.json()["error"])
    error.pop("trace_id", None)
    return error


# ---------------------------------------------------------------- 1. 计时侧信道


async def _login_seconds(env: TestEnv, username: str, password: str) -> float:
    """单次登录请求的墙钟耗时（顺带断言它确实是失败的）。"""
    started = time.perf_counter()
    response = await env.client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )
    elapsed = time.perf_counter() - started
    assert response.status_code == 401, response.text
    return elapsed


async def _interleaved_samples(
    env: TestEnv, *, repeats: int = 8
) -> tuple[list[float], list[float]]:
    """**交错**采样两条路径，返回 `(错口令样本, 无此人样本)`（首个样本含建连开销）。

    为什么用 min 而不是平均值/中位数：这台机器上单次请求的实测耗时在
    60-190ms 之间乱跳（asyncpg 连接池、日志、GC、并行跑着的其它测试都在
    掺和）。噪声**只加不减**，所以最小样本最接近"这条路径本身的成本"。
    实测同一路径重复 8 次，min 稳定落在 66-75ms（假 scrypt 取 60ms）。

    为什么要**交错**（A/B/A/B…）而不是先跑完 A 的 8 次再跑 B 的 8 次：全量
    CI 里负载是**随时间漂移**的，两段采样落在不同的负载段时，差值里就混进了
    "段间负载差"这个与代码无关的量——实测能到几十 ms，足以把"两条路径同量级"
    这个结论打穿（本地单文件连跑 3 次全绿，但全量跑偶尔红，就是这个原因）。
    交错之后每一对 A/B 彼此相邻、共享同一段负载，"两段之间"的漂移就不存在了，
    只剩"同一对内部"的残余——那部分改用**逐对差值**去度量（见 ④）。
    """
    wrong_samples: list[float] = []
    missing_samples: list[float] = []
    for _ in range(repeats):
        wrong_samples.append(await _login_seconds(env, "tester", "definitely-wrong"))
        missing_samples.append(
            await _login_seconds(env, "no-such-user-xyz", "definitely-wrong")
        )
    return wrong_samples, missing_samples


async def test_unknown_user_still_runs_scrypt(
    web_env: TestEnv, fake_scrypt: list[tuple[str, str]], limiter: LoginRateLimiter
) -> None:
    """用户不存在时**必须也跑一次 scrypt**（拿 dummy 哈希），耗时与密码错相当。

    变异验证：把 `stored_hash` 那段改回短路求值（用户不存在时不调用
    `verify_password`），这条用例立刻变红——调用次数少了一半，且
    "无此人"那条路径的 min 耗时掉到假 scrypt 之下。
    """
    repeats = 8
    wrong_samples, missing_samples = await _interleaved_samples(web_env, repeats=repeats)
    # 丢掉各自的首个：首次请求含 asyncpg 建连等一次性开销
    wrong_samples, missing_samples = wrong_samples[1:], missing_samples[1:]
    wrong_elapsed = min(wrong_samples)
    missing_elapsed = min(missing_samples)

    # ① 两条路径**每次**都做了口令校验：两轮共 2×repeats 次调用
    assert len(fake_scrypt) == 2 * repeats, (
        f"只调用了 {len(fake_scrypt)} 次 verify_password，"
        f"应为 {2 * repeats} 次——有路径跳过了 scrypt"
    )
    # ② 用户不存在那条用的是**固定 dummy 哈希**（不是 None、不是空串）。
    #    采样是交错的（错口令/无此人/错口令/无此人…），所以"无此人"落在
    #    奇数下标上；顺带确认"错口令"那条用的是真实哈希，不是两边都拿 dummy。
    from ling.infra.security import DUMMY_PASSWORD_HASH

    missing_hashes = [fake_scrypt[i][1] for i in range(1, len(fake_scrypt), 2)]
    assert missing_hashes and all(h == DUMMY_PASSWORD_HASH for h in missing_hashes), (
        "用户不存在的路径没有用 dummy 哈希"
    )
    wrong_hashes = [fake_scrypt[i][1] for i in range(0, len(fake_scrypt), 2)]
    assert all(h != DUMMY_PASSWORD_HASH for h in wrong_hashes), (
        "错口令路径本该拿真实哈希去比，却用了 dummy 哈希"
    )
    # ③ 两条路径的 min 耗时都**至少**覆盖一次假 scrypt——这就是
    #    "用户不存在也付了同样的 CPU 代价"的直接证据。
    #    跳过的路径会少掉整整 FAKE_SCRYPT_SECONDS。
    floor = FAKE_SCRYPT_SECONDS * 0.9
    assert missing_elapsed >= floor, (
        f"无此人路径 min 耗时 {missing_elapsed * 1000:.1f}ms，"
        f"低于假 scrypt {FAKE_SCRYPT_SECONDS * 1000:.0f}ms —— scrypt 被跳过了"
    )
    assert wrong_elapsed >= floor, (
        f"错口令路径 min 耗时 {wrong_elapsed * 1000:.1f}ms，低于假 scrypt"
    )
    # ④ 两条路径的耗时**不可区分**：至少有一对相邻的 A/B 采样彼此无差异。
    #    这里不用「两条路径各自的 min 相减」，理由是实测：这台机器被别的东西
    #    压住时，**单条**路径的 min 会被整体抬到 130ms 上下（假 scrypt 只要
    #    60ms，剩下 70ms 全是调度噪声），两条路径各自落在不同的"污染段"里，
    #    `|min(错口令) - min(无此人)|` 能到 34-60ms——纯噪声就足以打穿这条
    #    断言（实测过：全量跑偶发红，就是它）。逐对差值没有这个毛病：同一对
    #    两边的请求相隔几毫秒，要么都被污染（差值仍小），要么只有一边被污染
    #    （这一对被跳过，看下一对）。
    #    阈值 0.75×FAKE_SCRYPT_SECONDS（45ms）：比原来的一半更宽松（负载噪声
    #    只加不减，min 已是最优估计，但同一对内部仍有几十 ms 的残余），而真正
    #    要抓的回退是"整整少一次 scrypt"——回退后每一对都相差 ≈110ms（实测
    #    110/119ms），离 45ms 仍有成倍的安全距离。方向上是**变敏感**了：
    #    正常运行时逐对最小差值只有 0.6-3ms。
    pair_gaps = [abs(a - b) for a, b in zip(wrong_samples, missing_samples, strict=True)]
    smallest_gap = min(pair_gaps)
    assert smallest_gap < FAKE_SCRYPT_SECONDS * 0.75, (
        f"每一对采样都相差 ≥{smallest_gap * 1000:.1f}ms"
        f"（错口令 min {wrong_elapsed * 1000:.1f}ms / 无此人 min {missing_elapsed * 1000:.1f}ms），"
        "用户不存在与口令错的耗时仍可区分"
    )
    # ⑤ 错误体**完全一致**（不泄露是哪一种失败）
    wrong = await _login(web_env, "tester", "definitely-wrong")
    missing = await _login(web_env, "no-such-user-xyz", "definitely-wrong")
    assert _error_shape(wrong) == _error_shape(missing)


async def test_disabled_user_still_runs_scrypt(
    web_env: TestEnv, fake_scrypt: list[tuple[str, str]], limiter: LoginRateLimiter
) -> None:
    """被禁用的账号同样不能靠"跳过 scrypt"露出来。

    `user.disabled` 是另一个会让原短路求值提前返回的条件：禁用的账号如果
    不跑 scrypt，它和"口令错"的耗时差会暴露"这个账号存在且被禁用了"。
    """
    from sqlalchemy import select

    from ling.infra.models import User

    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.username == "tester"))
        ).scalar_one()
        user.disabled = True
        await session.flush()

    response = await _login(web_env, "tester", "tester-password")
    assert response.status_code == 401
    # 即便口令是**对的**，禁用账号也不能登录——且校验照跑
    assert len(fake_scrypt) == 1, "禁用账号的路径跳过了 scrypt"
    from ling.infra.security import DUMMY_PASSWORD_HASH

    assert fake_scrypt[0][1] == DUMMY_PASSWORD_HASH, "禁用账号的路径没有用 dummy 哈希"


# ---------------------------------------------------------------- 2. 限速


async def test_repeated_failures_are_rate_limited(
    web_env: TestEnv, fake_scrypt: list[tuple[str, str]], limiter: LoginRateLimiter
) -> None:
    """连续失败到阈值后被限速，**错误形状与口令错完全相同**。

    变异验证：去掉 `login()` 里 `retry_after` / `record_failure` 的接线，
    第 FAILURE_THRESHOLD+1 次仍会走数据库 + scrypt（调用记录会多一条），
    且 `retry_after` 一直是 0——这条用例变红。
    """
    # 这条用例要用**真实**阈值，不继承 fixture 里调高的那个。
    limiter.FAILURE_THRESHOLD = LoginRateLimiter.FAILURE_THRESHOLD
    limiter.IP_FAILURE_THRESHOLD = LoginRateLimiter.IP_FAILURE_THRESHOLD
    threshold = LoginRateLimiter.FAILURE_THRESHOLD
    failures = []
    for _ in range(threshold):
        r = await _login(web_env, "tester", "wrong-password")
        assert r.status_code == 401
        failures.append(_error_shape(r))
    assert limiter.retry_after(LoginRateLimiter.keys_for("tester", "testclient")) > 0

    # 第 threshold+1 次：被限速。错误体与前面**一模一样**
    locked = await _login(web_env, "tester", "wrong-password")
    assert locked.status_code == 401
    assert _error_shape(locked) == failures[-1], "限速响应泄露了锁定状态"
    # 被限速时不再做 scrypt（否则攻击者能用锁定状态放大 CPU 消耗）
    assert len(fake_scrypt) == threshold, "被限速的请求仍然跑了 scrypt"

    # 正确口令在锁定窗口内**也不能登进去**（否则限速形同虚设）
    correct = await _login(web_env, "tester", "tester-password")
    assert correct.status_code == 401


async def test_success_clears_failure_counter(
    web_env: TestEnv, fake_scrypt: list[tuple[str, str]], limiter: LoginRateLimiter
) -> None:
    """成功登录清零计数：4 次手滑之后输对，计数归零。"""
    for _ in range(LoginRateLimiter.FAILURE_THRESHOLD - 1):
        assert (await _login(web_env, "tester", "wrong-password")).status_code == 401

    ok = await _login(web_env, "tester", "tester-password")
    assert ok.status_code == 200
    keys = LoginRateLimiter.keys_for("tester", "testclient")
    assert limiter.retry_after(keys) == 0.0

    # 清零后重新从 1 开始：再错 4 次仍不锁（第 5 次才锁）
    for _ in range(LoginRateLimiter.FAILURE_THRESHOLD - 1):
        assert (await _login(web_env, "tester", "wrong-password")).status_code == 401
    assert limiter.retry_after(keys) == 0.0


# ---------------------------------------------------------------- 3. to_thread


async def test_scrypt_runs_off_the_event_loop(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch, limiter: LoginRateLimiter
) -> None:
    """scrypt 必须在**别的线程**里跑：事件循环要能在登录期间继续处理请求。

    断言方式：假 verify 记录执行它的线程名，同时在这 5ms 里让事件循环
    跑一个"心跳"任务——如果 scrypt 在主线程同步跑，心跳会被推迟。
    """
    threads: list[str] = []

    def _slow_verify(password: str, stored: str) -> bool:
        threads.append(threading.current_thread().name)
        time.sleep(0.05)  # 放大到 50ms，让"被卡住"足够可测
        return password == "tester-password" and not stored.startswith("scrypt$16384$8$1$bGluZy1kdW1teQ")

    import ling.domains.identity.service as svc

    monkeypatch.setattr(svc, "verify_password", _slow_verify)

    ticks: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        while not stop.is_set():
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.002)

    task = asyncio.create_task(heartbeat())
    await _login(web_env, "tester", "tester-password")
    stop.set()
    await task

    # ① 校验跑在主线程之外（to_thread 会开 worker 线程）
    assert threads, "verify_password 根本没被调用"
    assert all(t != threading.main_thread().name for t in threads), (
        f"scrypt 在主线程里跑了：{threads}"
    )
    # ② 这 50ms 期间事件循环没有停摆：心跳至少跳了 10 次（50/2ms，留余量）
    assert len(ticks) >= 10, f"登录期间事件循环被阻塞，心跳只跳了 {len(ticks)} 次"
    # ③ 相邻心跳间隔没有被一个 50ms 的空档撕开
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert max(gaps) < 0.05, f"出现 {max(gaps) * 1000:.0f}ms 的空档，事件循环被卡住"


async def test_concurrent_logins_do_not_serialize(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch, limiter: LoginRateLimiter
) -> None:
    """并发登录不该按"每人 50ms"串行叠加。

    `to_thread` 走默认线程池（本机 12 核 → 16 个 worker），5 个并发登录的
    墙钟应明显短于串行的 5×50ms。

    **不写死绝对毫秒数**：先在同一进程里量一遍串行基线，再要求并发比它
    快——绝对阈值会被机器负载、首次建连（asyncpg 冷启动）和 CI 抖动打穿。
    第一次调用是 warm-up，不计入基线（它会顺带把连接池填好）。
    """
    def _slow_verify(password: str, stored: str) -> bool:
        time.sleep(0.05)
        return password == "tester-password" and not stored.startswith("scrypt$16384$8$1$bGluZy1kdW1teQ")

    import ling.domains.identity.service as svc

    monkeypatch.setattr(svc, "verify_password", _slow_verify)

    # warm-up：连接池 + 首条查询 + 代码路径预热的开销不该算进基线
    await _login(web_env, "tester", "wrong-password")

    start = time.perf_counter()
    for _ in range(5):
        await _login(web_env, "tester", "wrong-password")
    serial = time.perf_counter() - start

    start = time.perf_counter()
    results = await asyncio.gather(
        *[_login(web_env, "tester", "wrong-password") for _ in range(5)]
    )
    parallel = time.perf_counter() - start

    assert all(r.status_code == 401 for r in results)
    # 串行 5×50ms=250ms 起步；并发若被事件循环串起来就会和基线一样慢
    assert parallel < serial * 0.6, (
        f"5 个并发登录 {parallel * 1000:.0f}ms vs 串行基线 {serial * 1000:.0f}ms，"
        "scrypt 仍在独占事件循环"
    )


# ---------------------------------------------------------------- 4. 语义诚实


async def test_limiter_is_per_app_instance_not_module_global(
    web_env: TestEnv, limiter: LoginRateLimiter
) -> None:
    """计数器挂在 `app.state` 上，不是模块全局单例。

    多副本部署时每个副本各有一份，所以阈值是 `副本数 × 阈值`——这个语义
    必须诚实。用例证明两个独立 limiter 互不影响（即"进程内"而不是"全局"）。
    """
    other = LoginRateLimiter()
    keys = LoginRateLimiter.keys_for("tester", "testclient")
    for _ in range(LoginRateLimiter.FAILURE_THRESHOLD):
        other.record_failure(keys)
    assert other.retry_after(keys) > 0
    assert limiter.retry_after(keys) == 0.0
    assert web_env.app.state.login_limiter is limiter  # type: ignore[attr-defined]
