/**
 * 交互判据，读取控件实现之前固定。
 *
 * 1. 用户选择音频后可以预听。发送动作只上传一次文件，并以 send 模式创建
 *    转写任务。请求体必须引用上传返回的 asset_id，并含稳定的请求标识。
 *    反例是没有接线、直接把文件名当 asset_id、重复点击产生多份任务。
 * 2. 转成文字创建 draft 模式任务。识别结果可编辑，提交携带用户修改后的
 *    文本与结果 revision，随后展示实际提交状态。反例是发送原识别文本。
 * 3. 刷新恢复已有 job 时只查询同一 job，禁止重新上传或新建转写任务。
 *    反例是只在 React 内存保存 job 或刷新时重新识别。
 * 4. 识别失败后显式重试同一 job，取消后清除恢复记录并允许重新选择文件。
 *    反例是重试重新上传、取消按钮只隐藏 UI 或页面刷新又恢复已取消任务。
 * 5. 上传、创建任务或轮询发生网络故障时给出可操作反馈。结果未知的创建
 *    请求重试必须复用请求标识。反例是每次重试生成新的 client_request_id。
 *
 * 我此前参与过控件实现，这份文件属于交互回归测试。
 * 独立验收与变异由未参与实现的人执行。
 */
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import { createServer, type ViteDevServer } from 'vite';
import react from '@vitejs/plugin-react';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const TIMEOUT = 60_000;
const SESSION_PATH = '/v1/sessions/s1';
const JOB_PATH = `${SESSION_PATH}/transcriptions/job-audio-1`;
const ASSET_ID = 'asset_browser_audio_1';
const CREATED = '2026-11-13T02:00:00Z';
let browser: Browser | null = null;
let server: ViteDevServer | null = null;
let baseUrl = '';
let launchError: string | null = null;
const contexts: BrowserContext[] = [];

beforeAll(async () => {
  server = await createServer({
    configFile: false, root: ROOT, plugins: [react()], logLevel: 'silent',
    server: { host: '127.0.0.1', port: 0 },
  });
  await server.listen();
  const address = server.httpServer?.address();
  if (!address || typeof address === 'string') throw new Error('Vite 没有可用监听地址');
  baseUrl = `http://127.0.0.1:${address.port}`;
  try { browser = await chromium.launch(); }
  catch (error) { launchError = error instanceof Error ? error.message : String(error); }
}, 90_000);

afterEach(async () => {
  await Promise.all(contexts.splice(0).map((context) => context.close()));
});
afterAll(async () => {
  await browser?.close();
  await server?.close();
});

function skipWithoutBrowser(context: { skip: () => void }): boolean {
  if (!launchError) return false;
  console.warn(`[audio-input] 跳过浏览器交互测试，${launchError}`);
  context.skip();
  return true;
}
function json(body: unknown, status = 200) {
  return { status, contentType: 'application/json', body: JSON.stringify(body) };
}
interface Job {
  job_id: string;
  state: string;
  submission: string;
  mode: 'send' | 'draft';
  revision: number;
  text: string;
  message_id: string | null;
  error: { code: string; message: string } | null;
}
interface Call {
  method: string;
  path: string;
  body: Record<string, unknown> | null;
  raw: Buffer | null;
  contentType: string;
}
interface BackendOptions {
  createState?: string;
  text?: string;
  revision?: number;
  abortCreateOnce?: boolean;
  abortUploadOnce?: boolean;
  abortPollOnce?: boolean;
  pollResult?: Partial<Job>;
  recording?: 'normal' | 'constructor-failure' | 'pending-permission';
  viewport?: { width: number; height: number };
}

/** 可播放的 PCM WAV，预听断言需要真实可解码音频。 */
function audioFixture(): Buffer {
  const sampleRate = 8000;
  const frames = 2400;
  const data = Buffer.alloc(44 + frames * 2);
  data.write('RIFF', 0);
  data.writeUInt32LE(data.length - 8, 4);
  data.write('WAVEfmt ', 8);
  data.writeUInt32LE(16, 16);
  data.writeUInt16LE(1, 20);
  data.writeUInt16LE(1, 22);
  data.writeUInt32LE(sampleRate, 24);
  data.writeUInt32LE(sampleRate * 2, 28);
  data.writeUInt16LE(2, 32);
  data.writeUInt16LE(16, 34);
  data.write('data', 36);
  data.writeUInt32LE(frames * 2, 40);
  for (let frame = 0; frame < frames; frame += 1) {
    data.writeInt16LE(Math.round(Math.sin(frame * Math.PI / 20) * 1000), 44 + frame * 2);
  }
  return data;
}
const AUDIO = audioFixture();

async function openChat(options: BackendOptions = {}) {
  if (!browser) throw new Error(launchError ?? '浏览器未启动');
  const context = await browser.newContext({
    reducedMotion: 'reduce', viewport: options.viewport ?? { width: 1200, height: 900 },
  });
  contexts.push(context);
  const page = await context.newPage();
  if (options.recording) await installRecorder(page, options.recording);
  const calls: Call[] = [];
  const recognitionKeys = new Set<string>();
  let current: Job | null = null;
  let abortCreate = options.abortCreateOnce ?? false;
  let abortUpload = options.abortUploadOnce ?? false;
  let abortPoll = options.abortPollOnce ?? false;

  await page.route('**/*', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    const contentType = request.headers()['content-type'] ?? '';
    if (path === `${SESSION_PATH}/audio-inputs` || path.startsWith(`${SESSION_PATH}/transcriptions`)) {
      const raw = request.postDataBuffer();
      calls.push({ path, method, raw, contentType,
        body: contentType.includes('application/json') ? request.postDataJSON() : null });
      if (path === `${SESSION_PATH}/audio-inputs` && method === 'POST') {
        if (abortUpload) { abortUpload = false; return route.abort('failed'); }
        return route.fulfill(json({ asset_id: ASSET_ID, duration_seconds: 0.3 }, 201));
      }
      if (path === `${SESSION_PATH}/transcriptions` && method === 'POST') {
        const body = request.postDataJSON();
        recognitionKeys.add(String(body.client_request_id));
        if (!current) {
          current = {
            job_id: 'job-audio-1', state: options.createState ?? 'queued', submission: 'pending',
            mode: body.mode, revision: options.revision ?? 1, text: options.text ?? '', message_id: null,
            error: options.createState === 'failed'
              ? { code: 'unavailable', message: '识别服务暂时不可用' } : null,
          };
        }
        // 服务端接收后丢失响应，验证客户端重试的幂等语义。
        if (abortCreate) { abortCreate = false; return route.abort('failed'); }
        return route.fulfill(json(current, 202));
      }
      if (!current) return route.fulfill(json({ error: { code: 'not_found', message: '任务不存在' } }, 404));
      if (path === JOB_PATH && method === 'GET') {
        if (abortPoll) { abortPoll = false; return route.abort('failed'); }
        current = { ...current, ...options.pollResult };
        return route.fulfill(json(current));
      }
      if (path === `${JOB_PATH}/retry` && method === 'POST') {
        current = { ...current, state: 'queued', error: null };
        return route.fulfill(json(current, 202));
      }
      if (path === JOB_PATH && method === 'DELETE') {
        current = { ...current, state: 'cancelled' };
        return route.fulfill(json(current));
      }
      if (path === `${JOB_PATH}/commit` && method === 'POST') {
        current = { ...current, mode: 'send', submission: 'pending', text: request.postDataJSON().text };
        return route.fulfill(json(current));
      }
      return route.fulfill(json({ error: { code: 'not_found', message: '请求路径无效' } }, 404));
    }
    if (path === '/v1/auth/me') {
      return route.fulfill(json({ schema_version: 'v1',
        user: { id: 'user-1', username: 'deploy', role: 'owner', subject_id: 'subject-owner', display_name: '主人' },
        config_revision: 1, test_mode: false, csrf_token: 'test-csrf' }));
    }
    if (path === '/v1/version') return route.fulfill(json({ current: '0.0.7' }));
    if (path === '/v1/persona') return route.fulfill(json({ name: '灵', display_name: '灵' }));
    if (path === '/v1/sessions') {
      return route.fulfill(json({ sessions: [{ schema_version: 'v1', session_id: 's1', actor_id: 'actor_ling',
        subject_id: 'subject-owner', window_id: 's1', title: '语音交互', created_at: CREATED,
        updated_at: CREATED, message_count: 0, archived: false }] }));
    }
    if (path === `${SESSION_PATH}/messages`) return route.fulfill(json({ messages: [], active_turn: null }));
    if (path.startsWith('/v1/')) {
      return route.fulfill(json({ error: { code: 'not_found', message: '测试未提供此资源', retriable: false } }, 404));
    }
    return route.continue();
  });
  await page.goto(`${baseUrl}/s/s1`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('group', { name: '语音输入', exact: true }).waitFor();
  await expect.poll(() => page.getByRole('button', { name: '上传语音', exact: true }).isEnabled()).toBe(true);
  return { page, calls, recognitionKeys };
}

async function chooseAudio(page: Page, buffer: Buffer = AUDIO): Promise<void> {
  const chooserPromise = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: '上传语音', exact: true }).click();
  const chooser = await chooserPromise;
  await chooser.setFiles({ name: 'input.wav', mimeType: 'audio/wav', buffer });
}
function matching(calls: Call[], method: string, path: string): Call[] {
  return calls.filter((call) => call.method === method && call.path === path);
}

async function installRecorder(page: Page, mode: NonNullable<BackendOptions['recording']>) {
  await page.addInitScript(({ selectedMode, audio }) => {
    const state = { stopped: 0, resolvePermission: null as (() => void) | null };
    Object.assign(window, { audioRecorderTest: state });
    const stream = { getTracks: () => [{ stop: () => { state.stopped += 1; } }] };
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
      getUserMedia: async () => {
        if (selectedMode === 'pending-permission') {
          return new Promise((resolve) => { state.resolvePermission = () => resolve(stream); });
        }
        return stream;
      },
    } });
    class TestRecorder {
      static isTypeSupported() { return true; }
      state = 'inactive';
      mimeType = 'audio/wav';
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor() {
        if (selectedMode === 'constructor-failure') throw new Error('test constructor failure');
      }
      start() { this.state = 'recording'; }
      stop() {
        this.state = 'inactive';
        this.ondataavailable?.({ data: new Blob([new Uint8Array(audio)], { type: this.mimeType }) });
        this.onstop?.();
      }
    }
    Object.defineProperty(window, 'MediaRecorder', { configurable: true, value: TestRecorder });
  }, { selectedMode: mode, audio: Array.from(AUDIO) });
}
async function recorderStops(page: Page): Promise<number> {
  return page.evaluate(() => (window as unknown as Window & { audioRecorderTest: { stopped: number } }).audioRecorderTest.stopped);
}
async function leaveChat(page: Page): Promise<void> {
  await page.evaluate(() => {
    history.pushState(null, '', '/settings');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await expect.poll(() => page.getByRole('group', { name: '语音输入', exact: true }).count()).toBe(0);
}

describe('Web 语音输入交互', () => {
  it('上传音频可预听，发送只提交一次真实文件与资产 ID', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page, calls, recognitionKeys } = await openChat({
      pollResult: { state: 'succeeded', submission: 'committed', text: '识别文本', message_id: 'message-audio' },
    });
    await chooseAudio(page);
    const preview = page.getByLabel('语音预听', { exact: true });
    await preview.waitFor();
    expect(await preview.getAttribute('src')).toMatch(/^blob:/);
    expect(await preview.evaluate(async (element) => {
      const audio = element as HTMLAudioElement;
      await audio.play();
      return audio.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && !audio.paused;
    })).toBe(true);
    await page.getByRole('button', { name: '发送语音', exact: true }).evaluate((element) => {
      const button = element as HTMLButtonElement;
      button.click();
      button.click();
    });
    await page.getByText('语音已处理', { exact: true }).waitFor();
    const uploads = matching(calls, 'POST', `${SESSION_PATH}/audio-inputs`);
    expect(uploads).toHaveLength(1);
    expect(uploads[0]!.contentType).toMatch(/^multipart\/form-data; boundary=/);
    expect(uploads[0]!.raw?.includes(AUDIO)).toBe(true);
    expect(uploads[0]!.raw?.toString('latin1')).toContain('name="file"; filename="input.wav"');
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`).map((call) => call.body)).toEqual([
      { asset_id: ASSET_ID, client_request_id: expect.stringMatching(/^[0-9a-f-]{36}$/i), mode: 'send' },
    ]);
    expect(recognitionKeys.size).toBe(1);
    expect(matching(calls, 'POST', `${JOB_PATH}/commit`)).toHaveLength(0);
    expect(await page.getByLabel('编辑转写文字', { exact: true }).count()).toBe(0);
  }, TIMEOUT);

  it('草稿提交使用用户编辑后的文字与服务端版本', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page, calls } = await openChat({ createState: 'succeeded', text: '识别原文', revision: 7,
      pollResult: { submission: 'committed', message_id: 'message-draft' } });
    await chooseAudio(page);
    await page.getByRole('button', { name: '转成文字', exact: true }).click();
    const draft = page.getByLabel('编辑转写文字', { exact: true });
    await expect.poll(() => draft.inputValue()).toBe('识别原文');
    await draft.fill('人工修正的文字，包含新的内容');
    await page.getByRole('button', { name: '发送转写文字', exact: true }).click();
    await page.getByText('语音已处理', { exact: true }).waitFor();
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`)[0]!.body?.mode).toBe('draft');
    expect(matching(calls, 'POST', `${JOB_PATH}/commit`).map((call) => call.body)).toEqual([
      { result_revision: 7, text: '人工修正的文字，包含新的内容' },
    ]);
  }, TIMEOUT);

  it('刷新只恢复原 job，取消后刷新不再恢复', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page, calls } = await openChat({ pollResult: { state: 'running' } });
    await chooseAudio(page);
    await page.getByRole('button', { name: '发送语音', exact: true }).click();
    await expect.poll(() => matching(calls, 'GET', JOB_PATH).length, { timeout: 5000 }).toBeGreaterThan(0);
    const beforeReload = matching(calls, 'GET', JOB_PATH).length;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: '取消识别', exact: true }).waitFor();
    await expect.poll(() => matching(calls, 'GET', JOB_PATH).length, { timeout: 5000 }).toBeGreaterThan(beforeReload);
    expect(matching(calls, 'POST', `${SESSION_PATH}/audio-inputs`)).toHaveLength(1);
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`)).toHaveLength(1);
    await page.getByRole('button', { name: '取消识别', exact: true }).click();
    await expect.poll(() => page.getByRole('button', { name: '上传语音', exact: true }).isEnabled()).toBe(true);
    const beforeSecondReload = matching(calls, 'GET', JOB_PATH).length;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect.poll(() => page.getByRole('button', { name: '上传语音', exact: true }).isEnabled()).toBe(true);
    expect(matching(calls, 'DELETE', JOB_PATH)).toHaveLength(1);
    expect(matching(calls, 'GET', JOB_PATH)).toHaveLength(beforeSecondReload);
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`)).toHaveLength(1);
  }, TIMEOUT);

  it('失败后的重试与取消作用于同一 job', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page, calls } = await openChat({ createState: 'failed' });
    await chooseAudio(page);
    await page.getByRole('button', { name: '转成文字', exact: true }).click();
    await page.getByText('识别服务暂时不可用', { exact: true }).waitFor();
    await page.getByRole('button', { name: '重试识别', exact: true }).click();
    await page.getByRole('button', { name: '取消识别', exact: true }).waitFor();
    expect(matching(calls, 'POST', `${JOB_PATH}/retry`)).toHaveLength(1);
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`)).toHaveLength(1);
    expect(matching(calls, 'POST', `${SESSION_PATH}/audio-inputs`)).toHaveLength(1);
    await page.getByRole('button', { name: '取消识别', exact: true }).click();
    await expect.poll(() => page.getByRole('button', { name: '上传语音', exact: true }).isEnabled()).toBe(true);
    expect(matching(calls, 'DELETE', JOB_PATH)).toHaveLength(1);
  }, TIMEOUT);

  for (const recovery of ['重试提交', '刷新页面'] as const) {
    it(`创建响应丢失后${recovery}复用资产与幂等标识`, async (context) => {
      if (skipWithoutBrowser(context)) return;
      const { page, calls, recognitionKeys } = await openChat({ abortCreateOnce: true,
        createState: 'succeeded', text: '请求接收后的识别结果' });
      await chooseAudio(page);
      await page.getByRole('button', { name: '转成文字', exact: true }).click();
      await page.getByRole('alert').filter({ hasText: '无法连接服务端' }).waitFor();
      if (recovery === '重试提交') await page.getByRole('button', { name: '重试提交', exact: true }).click();
      else await page.reload({ waitUntil: 'domcontentloaded' });
      await expect.poll(() => page.getByLabel('编辑转写文字', { exact: true }).inputValue()).toBe('请求接收后的识别结果');
      const creates = matching(calls, 'POST', `${SESSION_PATH}/transcriptions`);
      expect(creates).toHaveLength(2);
      expect(creates[1]!.body).toEqual(creates[0]!.body);
      expect(recognitionKeys.size).toBe(1);
      expect(matching(calls, 'POST', `${SESSION_PATH}/audio-inputs`)).toHaveLength(1);
    }, TIMEOUT);
  }

  it('上传网络失败保留音频，成功重试后才创建任务', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page, calls } = await openChat({ abortUploadOnce: true,
      createState: 'succeeded', text: '重试上传后的文本' });
    await chooseAudio(page);
    await page.getByRole('button', { name: '转成文字', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '无法连接服务端' }).waitFor();
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`)).toHaveLength(0);
    expect(await page.getByLabel('语音预听', { exact: true }).count()).toBe(1);
    await page.getByRole('button', { name: '转成文字', exact: true }).click();
    await expect.poll(() => page.getByLabel('编辑转写文字', { exact: true }).inputValue()).toBe('重试上传后的文本');
    expect(matching(calls, 'POST', `${SESSION_PATH}/audio-inputs`)).toHaveLength(2);
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`)).toHaveLength(1);
  }, TIMEOUT);

  it('轮询网络恢复后查询原任务并显示结果', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page, calls } = await openChat({ abortPollOnce: true,
      pollResult: { state: 'succeeded', text: '网络恢复后的文本', revision: 4 } });
    await chooseAudio(page);
    await page.getByRole('button', { name: '转成文字', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '无法连接服务端' }).waitFor();
    await expect.poll(() => page.getByLabel('编辑转写文字', { exact: true }).inputValue(), { timeout: 10_000 }).toBe('网络恢复后的文本');
    expect(matching(calls, 'GET', JOB_PATH)).toHaveLength(2);
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`)).toHaveLength(1);
    expect(matching(calls, 'POST', `${SESSION_PATH}/audio-inputs`)).toHaveLength(1);
    expect(await page.getByRole('alert').count()).toBe(0);
  }, TIMEOUT);

  it('识别失败但混合文字已提交时不再提供重试或移除任务', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page, calls } = await openChat({ pollResult: {
      state: 'failed', submission: 'committed', text: '随语音发送的文字',
      message_id: 'message-mixed', error: { code: 'unavailable', message: '识别失败，文字已提交' },
    } });
    await chooseAudio(page);
    await page.getByRole('button', { name: '发送语音', exact: true }).click();
    await page.getByText('语音已处理', { exact: true }).waitFor();
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`)).toHaveLength(1);
    expect(await page.getByRole('button', { name: '重试识别', exact: true }).count()).toBe(0);
    expect(await page.getByRole('button', { name: '移除语音任务', exact: true }).count()).toBe(0);
    expect(await page.getByRole('button', { name: '上传语音', exact: true }).isEnabled()).toBe(true);
  }, TIMEOUT);

  it.for(['succeeded', 'failed'])('任务停止处理后准确提示停止状态 %s', { timeout: TIMEOUT }, async (state, context) => {
    if (skipWithoutBrowser(context)) return;
    const { page } = await openChat({ pollResult: {
      state, submission: 'suppressed', text: '已经识别出的文本',
      error: { code: 'consent_revoked', message: '语音授权已撤销，本次内容未发送' },
    } });
    await chooseAudio(page);
    await page.getByRole('button', { name: '发送语音', exact: true }).click();
    await expect.poll(() => page.getByRole('button', { name: '上传语音', exact: true }).isEnabled(), { timeout: 5000 }).toBe(true);
    const status = await page.getByRole('group', { name: '语音输入', exact: true }).textContent();
    expect(status).toContain('语音处理已停止');
    expect(status).not.toContain('后续按会话状态处理');
    expect(await page.getByRole('button', { name: '重试识别', exact: true }).count()).toBe(0);
    expect(await page.getByRole('button', { name: '移除语音任务', exact: true }).count()).toBe(0);
  });

  it('超出字节上限与空音频在上传前给出反馈', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page, calls } = await openChat();
    await chooseAudio(page, Buffer.alloc(10 * 1024 * 1024 + 1));
    await page.getByRole('alert').filter({ hasText: '10 MiB' }).waitFor();
    expect(await page.getByRole('button', { name: '发送语音', exact: true }).count()).toBe(0);
    await chooseAudio(page, Buffer.alloc(0));
    await page.getByRole('alert').filter({ hasText: '语音文件为空' }).waitFor();
    expect(matching(calls, 'POST', `${SESSION_PATH}/audio-inputs`)).toHaveLength(0);
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`)).toHaveLength(0);
  }, TIMEOUT);

  it('录音停止释放麦克风，生成音频可预听和发送', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page, calls } = await openChat({ recording: 'normal',
      pollResult: { state: 'succeeded', submission: 'committed' } });
    await page.getByRole('button', { name: '录音', exact: true }).click();
    await page.getByRole('button', { name: '停止录音', exact: true }).click();
    await page.getByLabel('语音预听', { exact: true }).waitFor();
    expect(await recorderStops(page)).toBe(1);
    await page.getByRole('button', { name: '发送语音', exact: true }).click();
    await page.getByText('语音已处理', { exact: true }).waitFor();
    expect(matching(calls, 'POST', `${SESSION_PATH}/audio-inputs`)[0]!.raw?.includes(AUDIO)).toBe(true);
    expect(matching(calls, 'POST', `${SESSION_PATH}/transcriptions`)[0]!.body?.mode).toBe('send');
  }, TIMEOUT);

  it('录音中离开会话释放麦克风', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page } = await openChat({ recording: 'normal' });
    await page.getByRole('button', { name: '录音', exact: true }).click();
    await page.getByRole('button', { name: '停止录音', exact: true }).waitFor();
    await leaveChat(page);
    expect(await recorderStops(page)).toBe(1);
  }, TIMEOUT);

  it('录音器构造失败仍释放麦克风', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page } = await openChat({ recording: 'constructor-failure' });
    await page.getByRole('button', { name: '录音', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '录音无法启动' }).waitFor();
    expect(await recorderStops(page)).toBe(1);
    expect(await page.getByRole('button', { name: '上传语音', exact: true }).isEnabled()).toBe(true);
  }, TIMEOUT);

  it('授权等待期间离开，迟到的麦克风流立即释放', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page } = await openChat({ recording: 'pending-permission' });
    await page.getByRole('button', { name: '录音', exact: true }).click();
    await leaveChat(page);
    await page.evaluate(() => {
      (window as unknown as Window & { audioRecorderTest: { resolvePermission: () => void } }).audioRecorderTest.resolvePermission();
    });
    await expect.poll(() => recorderStops(page)).toBe(1);
  }, TIMEOUT);

  it('窄屏预听与文字编辑不造成页面横向溢出', async (context) => {
    if (skipWithoutBrowser(context)) return;
    const { page } = await openChat({ viewport: { width: 375, height: 812 },
      createState: 'succeeded', text: '窄屏语音文字' });
    await chooseAudio(page);
    await page.getByLabel('语音预听', { exact: true }).waitFor();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.getByRole('button', { name: '转成文字', exact: true }).click();
    await page.getByLabel('编辑转写文字', { exact: true }).waitFor();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  }, TIMEOUT);
});
