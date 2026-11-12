/**
 * 语音设置（⑤ 对话与主动性）的**真实浏览器**验证：改了的东西到底有没有进请求体。
 *
 * 为什么必须真浏览器：这一页的核心是"三家按 kind 切换字段"和"上传后把 asset_id
 * 填进表单"，两者**都是交互**。`renderToStaticMarkup` 能证明"画出了三个 radio"，
 * 证明不了点第二个之后 `input[name=voice-kind]` 真的换了、证明不了表单里那个
 * 看不见的 speed 被清掉了、更证明不了保存时请求体里带没带 `voice.reference_asset_id`。
 *
 * ## 这里的每条断言都盯着**请求体**，不盯屏幕
 *
 * 本项目反复出现"控件能看见、能点，但值从来没送到后端"。所以关键用例的结论一律
 * 取自 `PUT /v1/settings/draft` 的 body：
 * `dialogue.voice.kind` / `voice.voice_id` / `voice.reference_asset_id` /
 * `voice.speed` 是什么。界面文字只用于**辅助**断言"用户也能看到"。
 *
 * `/v1/**` 全部由 `page.route` 模拟（一个内存假后端）：不碰真后端、不写库。
 * 没有 playwright 浏览器时整组标成 skipped（不是静默 passed）并打印原因。
 */
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';

import { WILLINGNESS_PERSISTS } from './voiceSettings';
import { chromium, type Browser, type BrowserContext, type Page, type Route } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const ASSET_ID = 'ab'.repeat(32);
/**
 * 每条用例的超时。**必须显式给**：vitest 默认 5 秒，而本文件第一条用例要等
 * Vite 首次编译 + Chromium 首次导航；全量并发跑时它会超 5 秒，于是"产品坏了"
 * 和"机器忙"长得一模一样。与 `connectors.browser.test.tsx` 同一个常量名。
 */
const TIMEOUT = 60_000;

let browser: Browser | null = null;
let server: ViteDevServer | null = null;
let baseUrl = '';
let launchError: string | null = null;
const contexts: BrowserContext[] = [];

beforeAll(async () => {
  try {
    server = await createServer({
      configFile: false,
      root: ROOT,
      plugins: [react()],
      logLevel: 'silent',
      server: { host: '127.0.0.1', port: 0 },
    });
    await server.listen();
    const address = server.httpServer?.address();
    if (!address || typeof address === 'string') {
      throw new Error('Vite 没有给出可用的监听地址');
    }
    baseUrl = `http://127.0.0.1:${address.port}`;
    browser = await chromium.launch();
  } catch (error) {
    launchError = error instanceof Error ? error.message : String(error);
  }
}, 120_000);

afterEach(async () => {
  while (contexts.length > 0) {
    await contexts.pop()?.close();
  }
});

afterAll(async () => {
  await browser?.close();
  await server?.close();
});

function skip(ctx: { skip: () => void }): boolean {
  if (!launchError) return false;
  console.warn(`[voice-settings.browser] 跳过：${launchError}`);
  ctx.skip();
  return true;
}

/* ------------------------------------------------------------ 模拟后端 */

function json(body: unknown, status = 200): { status: number; contentType: string; body: string } {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

/** `GET /v1/settings/config` 里本页会读到的那一段（其余字段给最小值即可）。 */
interface FakeDialogue {
  voice_willingness?: string;
  voice?: Record<string, unknown>;
}

function configBody(dialogue: FakeDialogue) {
  const defaults = {
    group_turn_budget: 3,
    proactive_daily_limit: 2,
    quiet_start_minute: 0,
    quiet_end_minute: 0,
    private_may_stay_silent: true,
    group_mention_requires_reply: true,
    rest_may_skip: true,
    group_reply_delivery_enabled: false,
    proactive_delivery_enabled: false,
  };
  return {
    schema_version: 'v1',
    revision: 3,
    test_mode: false,
    log_access_denials: false,
    memory: {
      relative_score_floor: 0.5,
      rerank_top_n: 10,
      recency_half_life_days: 30,
      max_retention_days: 365,
    },
    life: {
      scene_ttl_minutes: 120,
      energy_decay_per_hour: 4,
      social_load_per_turn: 3,
      social_load_decay_per_hour: 6,
      max_live_plans: 4,
    },
    dialogue: { ...defaults, ...dialogue },
    providers: [],
    profiles: [],
    revisions: [],
    known_kinds: [],
    known_profiles: [],
    slots: [],
    profile_resolutions: [],
  };
}

interface DraftCall {
  /** `PUT /v1/settings/draft` 的 body，原样。 */
  body: Record<string, unknown>;
}

/** 上传计划：第一次按给定结果回答，之后按顺序。 */
type UploadPlan =
  | { kind: 'ok'; assetId?: string; format?: string; sizeBytes?: number }
  | { kind: 'error'; status: number; detail: string; message: string };

interface Harness {
  page: Page;
  drafts: DraftCall[];
  uploads: Array<{ field: string | null; filename: string | null; bytes: number }>;
  /** 让下一次 PUT 草稿返回给定的 `validation_errors`（模拟"草稿存下了但带错误"）。 */
  queueDraftIssues: (issues: Array<{ field: string; message: string; severity?: string }>) => void;
  /** 让下一次 PUT 草稿直接返回 HTTP 错误（带 `error.field`）。 */
  queueDraftHttpError: (status: number, field: string | null, message: string) => void;
}

async function openDialogue(
  dialogue: FakeDialogue = {},
  uploadPlans: UploadPlan[] = [],
): Promise<Harness> {
  if (!browser || launchError) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({ viewport: { width: 1440, height: 1200 }, reducedMotion: 'reduce' });
  contexts.push(context);
  const page = await context.newPage();
  const drafts: DraftCall[] = [];
  const uploads: Harness['uploads'] = [];
  const pendingIssues: Array<Array<{ field: string; message: string; severity?: string }>> = [];
  const pendingHttpErrors: Array<{ status: number; field: string | null; message: string }> = [];
  const plans = [...uploadPlans];

  await page.route('**/*', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (!path.startsWith('/v1/')) return route.continue();

    if (path === '/v1/auth/me') {
      return route.fulfill(
        json({
          schema_version: 'v1',
          user: {
            id: 'u1',
            username: 'demo',
            role: 'owner',
            subject_id: 's1',
            display_name: '主人',
            must_change_password: false,
          },
          config_revision: 1,
          test_mode: false,
          csrf_token: 'csrf',
        }),
      );
    }
    if (path === '/v1/version') return route.fulfill(json({ current: '0.0.7' }));
    if (path === '/v1/sessions') return route.fulfill(json({ sessions: [] }));
    if (path === '/v1/settings/config' && method === 'GET') {
      return route.fulfill(json(configBody(dialogue)));
    }
    if (path === '/v1/settings/taxonomy') return route.fulfill(json({ categories: [] }));

    if (path === '/v1/settings/draft' && method === 'PUT') {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      drafts.push({ body });
      const httpError = pendingHttpErrors.shift();
      if (httpError) {
        return route.fulfill(
          json(
            {
              error: {
                code: 'invalid_request',
                message: httpError.message,
                retriable: false,
                ...(httpError.field ? { field: httpError.field } : {}),
              },
            },
            httpError.status,
          ),
        );
      }
      return route.fulfill(
        json({
          revision: 4,
          state: 'draft',
          validation_errors: pendingIssues.shift() ?? [],
        }),
      );
    }
    if (path === '/v1/settings/publish' && method === 'POST') {
      return route.fulfill(json({ published_revision: 4 }));
    }

    if (path === '/v1/settings/speech/voice-reference' && method === 'POST') {
      // 从 multipart body 里读出**真实的**字段名与字节数：后端只认 `upload`。
      const body = route.request().postDataBuffer();
      const text = body ? body.toString('latin1') : '';
      const nameMatch = /name="([^"]+)"/.exec(text);
      const fileMatch = /filename="([^"]*)"/.exec(text);
      uploads.push({
        field: nameMatch?.[1] ?? null,
        filename: fileMatch?.[1] ?? null,
        bytes: body?.length ?? 0,
      });
      const plan = plans.shift();
      if (!plan) {
        return route.fulfill(json({ error: { code: 'internal', message: '没有为这次上传准备响应' } }, 500));
      }
      if (plan.kind === 'error') {
        return route.fulfill(
          json({ error: { code: 'invalid_request', message: plan.message, detail: plan.detail } }, plan.status),
        );
      }
      return route.fulfill(
        json({
          schema_version: 'v1',
          asset_id: plan.assetId ?? ASSET_ID,
          format: plan.format ?? 'wav',
          mime_type: 'audio/wav',
          size_bytes: plan.sizeBytes ?? 2048,
          duration_ms: null,
          retention_days: 0,
        }),
      );
    }

    return route.fulfill(json({ error: { code: 'not_found', message: path } }, 404));
  });

  await page.goto(`${baseUrl}/persona?tab=dialogue`, { waitUntil: 'domcontentloaded' });
  // 面板是异步读出来的（loading 之后才画），等它出现再操作。
  await page.getByTestId('voice-settings').waitFor({ timeout: 20_000 });

  return {
    page,
    drafts,
    uploads,
    queueDraftIssues: (issues) => pendingIssues.push(issues),
    queueDraftHttpError: (status, field, message) => pendingHttpErrors.push({ status, field, message }),
  };
}

/** 选一家音色。radio 在 `<label>` 里，点 label 更接近用户行为。 */
async function pickKind(page: Page, kind: 'minimax_tts' | 'fishaudio_tts' | 'mimo_tts' | 'none') {
  await page.getByTestId(`voice-kind-${kind}`).click();
}

async function save(page: Page) {
  await page.getByRole('button', { name: /保存并发布/ }).click();
}

/** 选中项的 value（读 DOM 状态，不是读文案）。 */
async function checkedKind(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const checked = document.querySelector<HTMLInputElement>('input[name="voice-kind"]:checked');
    return checked ? checked.value : null;
  });
}

async function checkedWillingness(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const checked = document.querySelector<HTMLInputElement>('input[name="voice-willingness"]:checked');
    return checked ? checked.value : null;
  });
}

/* ------------------------------------------------------------ 用例 */

describe('对话工具设置', () => {
  it('旧配置默认关闭，开启后随对话策略保存', async (ctx) => {
    if (skip(ctx)) return;
    const { page, drafts } = await openDialogue();
    const tools = page.getByRole('checkbox', { name: /允许对话调用工具/ });
    expect(await tools.isChecked()).toBe(false);

    await tools.check();
    await save(page);
    await expect.poll(() => drafts.length).toBe(1);
    const dialogue = drafts[0]?.body.dialogue as Record<string, unknown>;
    expect(dialogue.tools_enabled).toBe(1);
  }, TIMEOUT);
});

describe('语音设置：意愿档位', () => {
  it('三档都在，默认 natural，且每档下面有一句说明', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDialogue();
    for (const value of ['reserved', 'natural', 'expressive']) {
      await expect.poll(() => page.getByTestId(`voice-willingness-input-${value}`).count()).toBe(1);
    }
    expect(await checkedWillingness(page)).toBe('natural');
    const text = await page.getByTestId('voice-willingness').textContent();
    expect(text).toContain('你通常偏向文字');
    expect(text).toContain('你根据当下交流自然选择');
    expect(text).toContain('更愿意开口说话');
    // 要档位，不要数字滑块。
    expect(await page.locator('input[type="range"]').count()).toBe(0);
  }, TIMEOUT);

  it('回读：库里是 expressive 时选中 expressive，不是回到 natural', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDialogue({ voice_willingness: 'expressive' });
    expect(await checkedWillingness(page)).toBe('expressive');
  }, TIMEOUT);

  it('换档 → 保存：请求体里 dialogue.voice_willingness 是选中的那一档', async (ctx) => {
    if (skip(ctx)) return;
    const { page, drafts } = await openDialogue({ voice_willingness: 'natural' });
    await page.getByTestId('voice-willingness-reserved').click();
    expect(await checkedWillingness(page)).toBe('reserved');

    await save(page);
    await expect.poll(() => drafts.length).toBe(1);
    const dialogue = drafts[0]?.body.dialogue as Record<string, unknown>;
    expect(dialogue.voice_willingness).toBe('reserved');
  }, TIMEOUT);

  it('档位能不能存，页面必须如实说（不许画一个能点却不生效的控件）', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDialogue();
    const warn = page.getByTestId('voice-willingness-not-persisted');
    /*
     * 两条分支都要留：这个开关的作用是"能力边界变了，界面跟着改口"。
     *
     * - `WILLINGNESS_PERSISTS === false`（写入入口没这个字段时）→ 必须显示
     *   警告，否则用户会以为选了就生效。
     * - `WILLINGNESS_PERSISTS === true`（`d1564c8` 之后）→ **必须不显示**。
     *   留着它反而是另一种假话：说"存不住"而其实存得住。
     *
     * 断言写成跟随常量，而不是钉死某一侧：把开关翻回去时这条会自动变成
     * "必须显示警告"，不需要有人记得改测试。
     */
    if (WILLINGNESS_PERSISTS) {
      await expect.poll(() => warn.count()).toBe(0);
    } else {
      await expect.poll(() => warn.count()).toBe(1);
      expect(await warn.textContent()).toContain('刷新后会回到原来的值');
    }
  }, TIMEOUT);
});

describe('语音设置：三家字段按 kind 切换', () => {
  it('未选 kind：一个音色字段都不显示，并说清"只会发文字"', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDialogue();
    await expect.poll(() => page.getByTestId('voice-not-configured').count()).toBe(1);
    for (const field of ['voice_id', 'minimax-params', 'reference_asset_id']) {
      expect(await page.getByTestId(`voice-field-${field}`).count()).toBe(0);
    }
  }, TIMEOUT);

  it('minimax：voice_id + 语速/音量/音高/情绪 全部出现', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDialogue();
    await pickKind(page, 'minimax_tts');
    expect(await checkedKind(page)).toBe('minimax_tts');
    await expect.poll(() => page.getByTestId('voice-field-voice_id').count()).toBe(1);
    await expect.poll(() => page.getByTestId('voice-input-speed').count()).toBe(1);
    await expect.poll(() => page.getByTestId('voice-input-pitch').count()).toBe(1);
    await expect.poll(() => page.getByTestId('voice-input-emotion').count()).toBe(1);
    expect(await page.getByTestId('voice-field-reference_asset_id').count()).toBe(0);
  }, TIMEOUT);

  it('fishaudio：**只有** voice_id，不显示语速/音量/音高/情绪', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDialogue();
    await pickKind(page, 'fishaudio_tts');
    await expect.poll(() => page.getByTestId('voice-field-voice_id').count()).toBe(1);
    for (const field of ['speed', 'vol', 'pitch', 'emotion']) {
      expect(await page.getByTestId(`voice-input-${field}`).count()).toBe(0);
    }
    expect(await page.getByTestId('voice-field-minimax-params').count()).toBe(0);
  }, TIMEOUT);

  it('mimo：只有参考音频上传，**不显示 speed/vol/pitch**，也不显示 voice_id', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDialogue();
    await pickKind(page, 'mimo_tts');
    await expect.poll(() => page.getByTestId('voice-field-reference_asset_id').count()).toBe(1);
    await expect.poll(() => page.getByTestId('voice-upload-input').count()).toBe(1);
    expect(await page.getByTestId('voice-input-speed').count()).toBe(0);
    expect(await page.getByTestId('voice-input-vol').count()).toBe(0);
    expect(await page.getByTestId('voice-input-pitch').count()).toBe(0);
    expect(await page.getByTestId('voice-field-voice_id').count()).toBe(0);
  }, TIMEOUT);
});

describe('语音设置：切 kind 清掉旧家的字段', () => {
  it('minimax（填了语速/音量/音高）→ mimo：切回来的 speed 是默认值，不是刚才填的', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDialogue();
    await pickKind(page, 'minimax_tts');
    await page.getByTestId('voice-input-speed').fill('1.7');
    await page.getByTestId('voice-input-vol').fill('0.4');
    await page.getByTestId('voice-input-pitch').fill('5');

    await pickKind(page, 'mimo_tts');
    // 屏幕上已经看不到它们了。但"看不见"不等于"被清掉"——关键在下面切回来那一下：
    await pickKind(page, 'minimax_tts');
    await expect.poll(() => page.getByTestId('voice-input-speed').inputValue()).toBe('1');
    expect(await page.getByTestId('voice-input-vol').inputValue()).toBe('1');
    expect(await page.getByTestId('voice-input-pitch').inputValue()).toBe('0');
  }, TIMEOUT);

  it('minimax → mimo → 填参考音频 → 保存：请求体里**没有** speed/vol/pitch/voice_id', async (ctx) => {
    if (skip(ctx)) return;
    const { page, drafts } = await openDialogue({}, [{ kind: 'ok' }]);
    await pickKind(page, 'minimax_tts');
    await page.getByTestId('voice-input-speed').fill('1.9');
    await pickKind(page, 'mimo_tts');
    await page.getByTestId('voice-upload-input').setInputFiles({
      name: 'ref.wav',
      mimeType: 'audio/wav',
      buffer: Buffer.from('RIFF....WAVEfmt '),
    });
    await expect.poll(() => page.getByTestId('voice-upload-result').count()).toBe(1);

    await save(page);
    await expect.poll(() => drafts.length).toBe(1);
    const voice = (drafts[0]?.body.dialogue as Record<string, unknown>).voice as Record<string, unknown>;
    expect(voice.kind).toBe('mimo_tts');
    expect(voice.reference_asset_id).toBe(ASSET_ID);
    // 这三条就是"残留字段被带上去"的形状：界面上看不见，但请求体里在。
    expect(voice).not.toHaveProperty('speed');
    expect(voice).not.toHaveProperty('vol');
    expect(voice).not.toHaveProperty('pitch');
    expect(voice).not.toHaveProperty('voice_id');
  }, TIMEOUT);

  it('minimax → fishaudio：voice_id 保住，四个参数不出现在请求体里', async (ctx) => {
    if (skip(ctx)) return;
    const { page, drafts } = await openDialogue();
    await pickKind(page, 'minimax_tts');
    await page.getByTestId('voice-input-voice_id').fill('mm-abc');
    await page.getByTestId('voice-input-speed').fill('1.6');
    await page.getByTestId('voice-input-emotion').fill('happy');

    await pickKind(page, 'fishaudio_tts');
    // 标签跟着改成那边叫法。
    expect(await page.getByTestId('voice-field-voice_id').textContent()).toContain('reference_id');
    // id 没被清掉（同一件东西换了个名字），参数则一个都不发。
    expect(await page.getByTestId('voice-input-voice_id').inputValue()).toBe('mm-abc');

    await save(page);
    await expect.poll(() => drafts.length).toBe(1);
    const voice = (drafts[0]?.body.dialogue as Record<string, unknown>).voice as Record<string, unknown>;
    expect(voice).toEqual({ kind: 'fishaudio_tts', voice_id: 'mm-abc' });
  }, TIMEOUT);
});

describe('语音设置：MiMo 参考音频上传', () => {
  it('上传成功 → asset_id 填进表单 → 保存请求体里带它', async (ctx) => {
    if (skip(ctx)) return;
    const { page, drafts, uploads } = await openDialogue({}, [{ kind: 'ok', assetId: ASSET_ID }]);
    await pickKind(page, 'mimo_tts');
    await page.getByTestId('voice-upload-input').setInputFiles({
      name: 'my-voice.wav',
      mimeType: 'audio/wav',
      buffer: Buffer.from('RIFF0).00'),
    });

    // 上传结果：格式 + 大小（人话），以及填进表单的素材。
    await expect.poll(() => page.getByTestId('voice-upload-result').count()).toBe(1);
    const result = await page.getByTestId('voice-upload-result').textContent();
    expect(result).toContain('WAV');
    expect(result).toContain('2.0 KB');
    expect(await page.getByTestId('voice-reference-asset').textContent()).toContain(ASSET_ID.slice(0, 8));

    // 字段名必须是后端要的 `upload`：换个名字后端收不到。
    expect(uploads).toHaveLength(1);
    expect(uploads[0]?.field).toBe('upload');
    expect(uploads[0]?.filename).toBe('my-voice.wav');

    await save(page);
    await expect.poll(() => drafts.length).toBe(1);
    const voice = (drafts[0]?.body.dialogue as Record<string, unknown>).voice as Record<string, unknown>;
    expect(voice.kind).toBe('mimo_tts');
    expect(voice.reference_asset_id).toBe(ASSET_ID);
  }, TIMEOUT);

  it('上传失败 → 说清原因（413 给 MB）→ 可以重试并成功', async (ctx) => {
    if (skip(ctx)) return;
    const { page, drafts, uploads } = await openDialogue({}, [
      { kind: 'error', status: 413, detail: 'voice_reference_too_large', message: '参考音频超过 10MB 上限' },
      { kind: 'ok', assetId: ASSET_ID },
    ]);
    await pickKind(page, 'mimo_tts');
    await page.getByTestId('voice-upload-input').setInputFiles({
      name: 'huge.wav',
      mimeType: 'audio/wav',
      buffer: Buffer.from('RIFF'),
    });

    await expect.poll(() => page.getByTestId('voice-upload-error').count()).toBe(1);
    const reason = await page.getByTestId('voice-upload-error-reason').textContent();
    expect(reason).toContain('10');
    expect(reason).toContain('MB');
    // 失败后**没有**素材被填进去：失败不是"部分成功"。
    expect(await page.getByTestId('voice-reference-missing').count()).toBe(1);

    // 重试：按钮还在、可点，再选一次就成功。
    const retry = page.getByTestId('voice-upload-retry');
    await expect.poll(() => retry.isEnabled()).toBe(true);
    await page.getByTestId('voice-upload-input').setInputFiles({
      name: 'good.wav',
      mimeType: 'audio/wav',
      buffer: Buffer.from('RIFFgood'),
    });
    await expect.poll(() => page.getByTestId('voice-upload-result').count()).toBe(1);
    expect(await page.getByTestId('voice-upload-error').count()).toBe(0);
    expect(uploads).toHaveLength(2);

    await save(page);
    await expect.poll(() => drafts.length).toBe(1);
    const voice = (drafts[0]?.body.dialogue as Record<string, unknown>).voice as Record<string, unknown>;
    expect(voice.reference_asset_id).toBe(ASSET_ID);
  }, TIMEOUT);

  it('上传失败（422 不是音频）→ 列出支持的格式', async (ctx) => {
    if (skip(ctx)) return;
    const { page } = await openDialogue({}, [
      {
        kind: 'error',
        status: 422,
        detail: 'voice_reference_invalid_audio',
        message: '这段内容不是可识别的音频',
      },
    ]);
    await pickKind(page, 'mimo_tts');
    await page.getByTestId('voice-upload-input').setInputFiles({
      name: 'notes.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('not audio at all'),
    });
    await expect.poll(() => page.getByTestId('voice-upload-error').count()).toBe(1);
    const box = await page.getByTestId('voice-upload-error').textContent();
    for (const fmt of ['wav', 'mp3', 'flac', 'ogg', 'm4a']) {
      expect(box).toContain(fmt);
    }
  }, TIMEOUT);
});

describe('语音设置：保存前拦截与字段级错误', () => {
  it('选了 minimax 但没填 voice_id → 保存被拦，一个 PUT 都没发', async (ctx) => {
    if (skip(ctx)) return;
    const { page, drafts } = await openDialogue();
    await pickKind(page, 'minimax_tts');
    await save(page);

    await expect.poll(() => page.getByTestId('voice-error-voice_id').count()).toBe(1);
    expect(await page.getByTestId('voice-error-voice_id').textContent()).toContain('voice_id');
    // 关键：**没有发请求**。发出去再被后端拒，就是另一个故事了。
    expect(drafts).toHaveLength(0);
  }, TIMEOUT);

  it('选了 mimo 但没上传 → 保存被拦，一个 PUT 都没发', async (ctx) => {
    if (skip(ctx)) return;
    const { page, drafts } = await openDialogue();
    await pickKind(page, 'mimo_tts');
    await save(page);
    await expect.poll(() => page.getByTestId('voice-error-reference_asset_id').count()).toBe(1);
    expect(drafts).toHaveLength(0);
  }, TIMEOUT);

  it('后端在 validation_errors 里报 voice.voice_id → 标在那个输入框上，不是只丢横幅', async (ctx) => {
    if (skip(ctx)) return;
    const { page, drafts, queueDraftIssues } = await openDialogue();
    queueDraftIssues([
      { field: 'dialogue.voice.voice_id', message: '音色类型是 minimax_tts，但没填 voice_id', severity: 'error' },
    ]);
    await pickKind(page, 'minimax_tts');
    await page.getByTestId('voice-input-voice_id').fill('something');
    await save(page);
    await expect.poll(() => drafts.length).toBe(1);
    await expect.poll(() => page.getByTestId('voice-error-voice_id').count()).toBe(1);
    expect(await page.getByTestId('voice-error-voice_id').textContent()).toContain('minimax_tts');
    // 草稿没通过校验就不该发布。
    expect(page.url()).not.toContain('published');
  }, TIMEOUT);
});
