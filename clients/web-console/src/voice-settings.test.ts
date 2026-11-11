/**
 * 语音设置的**纯逻辑**：档位、三家字段归属、切 kind 清理、请求体组装、上传失败文案。
 *
 * 这里钉的是 `*.browser.test.tsx` 看不到的东西：**中间状态**。
 * 浏览器测试能证明"点了 mimo 之后屏幕上没有语速"，证明不了"表单里那个 speed
 * 真的被清成了空"——因为界面不画它。而正是那个看不见的残留值会在切回
 * MiniMax 时冒出来，被当成"我刚填的"。
 */
import { describe, expect, it } from 'vitest';

import {
  blockingSaveReason,
  EMPTY_VOICE_DRAFT,
  explainUploadFailure,
  fieldsForKind,
  formatBytes,
  MAX_VOICE_BYTES,
  normalizeWillingness,
  switchVoiceKind,
  VOICE_WILLINGNESS_OPTIONS,
  voiceDraftFrom,
  voiceFieldOf,
  voiceIdLabel,
  voiceIssuesFromDraft,
  voicePayloadFrom,
  voiceIssueFromError,
  type VoiceDraft,
} from './voiceSettings';
import { ApiError } from './api';

const MINIMAX_FILLED: VoiceDraft = {
  kind: 'minimax_tts',
  voice_id: 'mm-voice-001',
  reference_asset_id: '',
  speed: '1.4',
  vol: '0.8',
  pitch: '3',
  emotion: 'happy',
};

describe('意愿档位：三档、语义、不抽随机数', () => {
  it('恰好三档，顺序稳定，默认档是 natural', () => {
    expect(VOICE_WILLINGNESS_OPTIONS.map((option) => option.value)).toEqual([
      'reserved',
      'natural',
      'expressive',
    ]);
  });

  it('每个档位都有一句人话说明，且三档的说明互不相同', () => {
    const details = VOICE_WILLINGNESS_OPTIONS.map((option) => option.detail);
    for (const detail of details) {
      // 空文案或只有标点的"说明"等于没说明。
      expect(detail.length).toBeGreaterThan(20);
    }
    expect(new Set(details).size).toBe(3);
  });

  it('说明里不出现"百分比 / 概率为 N% / 随机"这类把倾向当概率的说法', () => {
    for (const option of VOICE_WILLINGNESS_OPTIONS) {
      expect(option.detail).not.toMatch(/\d+\s*%/);
      expect(option.detail).not.toContain('随机');
    }
  });

  it('未知值收敛成 natural，不是崩掉也不是 reserved', () => {
    expect(normalizeWillingness('reserved')).toBe('reserved');
    expect(normalizeWillingness('expressive')).toBe('expressive');
    expect(normalizeWillingness('natural')).toBe('natural');
    expect(normalizeWillingness(undefined)).toBe('natural');
    expect(normalizeWillingness('high')).toBe('natural');
    expect(normalizeWillingness(3)).toBe('natural');
  });
});

describe('三家字段归属：每家只属于自己那几个', () => {
  it('minimax 有 id + 四个参数', () => {
    expect(fieldsForKind('minimax_tts')).toEqual(['voice_id', 'speed', 'vol', 'pitch', 'emotion']);
  });

  it('fishaudio **只有** voice_id（没有 speed/vol/pitch）', () => {
    expect(fieldsForKind('fishaudio_tts')).toEqual(['voice_id']);
    expect(fieldsForKind('fishaudio_tts')).not.toContain('speed');
    expect(fieldsForKind('fishaudio_tts')).not.toContain('vol');
    expect(fieldsForKind('fishaudio_tts')).not.toContain('pitch');
  });

  it('mimo **只有** reference_asset_id（没有 speed/vol/pitch，也没有 voice_id）', () => {
    expect(fieldsForKind('mimo_tts')).toEqual(['reference_asset_id']);
  });

  it('未选 kind 时一个字段都不属于它', () => {
    expect(fieldsForKind(null)).toEqual([]);
  });

  it('voice_id 在 FishAudio 那边叫 reference_id（与后端报错用同一套词）', () => {
    expect(voiceIdLabel('fishaudio_tts')).toBe('reference_id');
    expect(voiceIdLabel('minimax_tts')).toBe('voice_id');
  });
});

describe('切 kind：旧家的字段被**真的**清掉（不是只是不显示）', () => {
  it('minimax → mimo：speed/vol/pitch/emotion/voice_id 全部清空', () => {
    const next = switchVoiceKind(MINIMAX_FILLED, 'mimo_tts');
    expect(next.kind).toBe('mimo_tts');
    expect(next.voice_id).toBe('');
    expect(next.speed).toBe('');
    expect(next.vol).toBe('');
    expect(next.pitch).toBe('');
    expect(next.emotion).toBe('');
  });

  it('minimax → fishaudio：保住 voice_id，四个参数清空', () => {
    const next = switchVoiceKind(MINIMAX_FILLED, 'fishaudio_tts');
    expect(next.voice_id).toBe('mm-voice-001');
    expect(next.speed).toBe('');
    expect(next.vol).toBe('');
    expect(next.pitch).toBe('');
    expect(next.emotion).toBe('');
  });

  it('fishaudio → minimax：参数回到默认值（不是留着空，也不是沿用别家）', () => {
    const fish: VoiceDraft = { ...EMPTY_VOICE_DRAFT, kind: 'fishaudio_tts', voice_id: 'fa-ref' };
    const next = switchVoiceKind(fish, 'minimax_tts');
    expect(next.speed).toBe('1');
    expect(next.vol).toBe('1');
    expect(next.pitch).toBe('0');
    expect(next.emotion).toBe('');
    expect(next.voice_id).toBe('fa-ref');
  });

  it('mimo → 不使用音色：连 reference_asset_id 也清掉', () => {
    const mimo: VoiceDraft = {
      ...EMPTY_VOICE_DRAFT,
      kind: 'mimo_tts',
      reference_asset_id: 'a'.repeat(64),
    };
    const next = switchVoiceKind(mimo, null);
    expect(next).toEqual({ ...EMPTY_VOICE_DRAFT, kind: null });
  });

  it('切到同一家是幂等的：不会把用户刚填的 id 清掉', () => {
    expect(switchVoiceKind(MINIMAX_FILLED, 'minimax_tts')).toBe(MINIMAX_FILLED);
  });

  it('切到 mimo 再切回 minimax：拿到的是**默认值**，旧家的 1.4/0.8/3 不会复活', () => {
    const there = switchVoiceKind(MINIMAX_FILLED, 'mimo_tts');
    const back = switchVoiceKind(there, 'minimax_tts');
    expect(back.speed).toBe('1');
    expect(back.vol).toBe('1');
    expect(back.pitch).toBe('0');
    expect(back.emotion).toBe('');
  });
});

describe('请求体：只发这一家拥有的字段', () => {
  it('minimax 发 id 与四个参数，数字是真数字（不是字符串）', () => {
    expect(voicePayloadFrom(MINIMAX_FILLED)).toEqual({
      kind: 'minimax_tts',
      voice_id: 'mm-voice-001',
      speed: 1.4,
      vol: 0.8,
      pitch: 3,
      emotion: 'happy',
    });
  });

  it('fishaudio 只发 kind 与 voice_id —— 一个 speed/vol/pitch 都没有', () => {
    // 故意塞进残留值：**即使草稿里有**，也不该发出去。
    const dirty: VoiceDraft = { ...MINIMAX_FILLED, kind: 'fishaudio_tts' };
    const payload = voicePayloadFrom(dirty);
    expect(payload).toEqual({ kind: 'fishaudio_tts', voice_id: 'mm-voice-001' });
    expect(payload).not.toHaveProperty('speed');
    expect(payload).not.toHaveProperty('vol');
    expect(payload).not.toHaveProperty('pitch');
  });

  it('mimo 只发 kind 与 reference_asset_id', () => {
    const asset = 'b'.repeat(64);
    const dirty: VoiceDraft = {
      ...MINIMAX_FILLED,
      kind: 'mimo_tts',
      reference_asset_id: asset,
    };
    const payload = voicePayloadFrom(dirty);
    expect(payload).toEqual({ kind: 'mimo_tts', reference_asset_id: asset });
    expect(payload).not.toHaveProperty('speed');
    expect(payload).not.toHaveProperty('voice_id');
  });

  it('未选 kind：发 {kind: null}（这是"取消音色"，后端认得），不带任何残留', () => {
    expect(voicePayloadFrom({ ...MINIMAX_FILLED, kind: null })).toEqual({ kind: null });
  });

  it('emotion 留空就不发这一项（不猜情绪），不是发空串', () => {
    const payload = voicePayloadFrom({ ...MINIMAX_FILLED, emotion: '  ' });
    expect(payload).not.toHaveProperty('emotion');
  });

  it('数字清空时回落默认值；pitch 取整', () => {
    const payload = voicePayloadFrom({ ...MINIMAX_FILLED, speed: '', vol: '', pitch: '2.6' });
    expect(payload.speed).toBe(1);
    expect(payload.vol).toBe(1);
    expect(payload.pitch).toBe(3);
  });

  it('非数字输入不会写出 NaN（NaN 会让整个 JSON 字段变 null）', () => {
    const payload = voicePayloadFrom({ ...MINIMAX_FILLED, speed: 'abc' });
    expect(payload.speed).toBe(1);
  });
});

describe('读回：不跨家预填、不编默认', () => {
  it('从没配过 → kind 为空，一个字段都不属于它', () => {
    expect(voiceDraftFrom(undefined).kind).toBeNull();
    expect(voiceDraftFrom(null).kind).toBeNull();
    expect(fieldsForKind(voiceDraftFrom(null).kind)).toEqual([]);
  });

  it('库里是 mimo 时，别的家的 voice_id 不会被搬过来变成 mimo 的 id', () => {
    const draft = voiceDraftFrom({
      kind: 'mimo_tts',
      voice_id: 'stale-minimax-id',
      reference_asset_id: 'c'.repeat(64),
      speed: 1,
      vol: 1,
      pitch: 0,
      emotion: null,
    });
    expect(draft.kind).toBe('mimo_tts');
    // 读回原样保留（后端不报错、前端也不该假装它不存在），但它不属于 mimo。
    expect(fieldsForKind(draft.kind)).not.toContain('voice_id');
  });

  it('未知 kind 当成没选（而不是猜一个）', () => {
    expect(voiceDraftFrom({ kind: 'azure_tts' } as unknown as Parameters<typeof voiceDraftFrom>[0]).kind).toBeNull();
  });
});

describe('保存前拦截：缺 id 就在本地拦，不用等后端 422', () => {
  it('选了 minimax 但 voice_id 为空 → 拦，并指向 voice_id', () => {
    const blocked = blockingSaveReason({ ...MINIMAX_FILLED, voice_id: '   ' });
    expect(blocked?.field).toBe('dialogue.voice.voice_id');
    expect(blocked?.message).toContain('minimax_tts');
    expect(blocked?.message).toContain('voice_id');
  });

  it('选了 fishaudio 但没填 → 拦，措辞用 reference_id', () => {
    const blocked = blockingSaveReason({ ...EMPTY_VOICE_DRAFT, kind: 'fishaudio_tts' });
    expect(blocked?.field).toBe('dialogue.voice.voice_id');
    expect(blocked?.message).toContain('reference_id');
  });

  it('选了 mimo 但没上传 → 拦，并指向 reference_asset_id', () => {
    const blocked = blockingSaveReason({ ...EMPTY_VOICE_DRAFT, kind: 'mimo_tts' });
    expect(blocked?.field).toBe('dialogue.voice.reference_asset_id');
    expect(blocked?.message).toContain('参考音频');
  });

  it('填全了 → 不拦', () => {
    expect(blockingSaveReason(MINIMAX_FILLED)).toBeNull();
    expect(
      blockingSaveReason({ ...EMPTY_VOICE_DRAFT, kind: 'mimo_tts', reference_asset_id: 'd'.repeat(64) }),
    ).toBeNull();
  });

  it('未选 kind → 不拦：不配音色是合法状态，不是"配错了"', () => {
    expect(blockingSaveReason(EMPTY_VOICE_DRAFT)).toBeNull();
  });
});

describe('字段级错误：把 error.field 收到对应输入框', () => {
  it('把全路径收敛成短名，不认识的 prefix 返回 null（不误标）', () => {
    expect(voiceFieldOf('dialogue.voice.voice_id')).toBe('voice_id');
    expect(voiceFieldOf('dialogue.voice.reference_asset_id')).toBe('reference_asset_id');
    expect(voiceFieldOf('dialogue.voice.kind')).toBe('kind');
    expect(voiceFieldOf('dialogue.voice.speed')).toBe('speed');
    expect(voiceFieldOf('dialogue.group_turn_budget')).toBeNull();
    expect(voiceFieldOf('memory.relative_score_floor')).toBeNull();
  });

  it('草稿的 validation_errors：只取音色那几条，warning 不算', () => {
    const issues = voiceIssuesFromDraft([
      { field: 'dialogue.voice.voice_id', message: '没填 voice_id', severity: 'error' },
      { field: 'dialogue.voice.kind', message: '填了参数却没选类型', severity: 'error' },
      { field: 'settings.providers.x.secret', message: '没存密钥', severity: 'warning' },
      { field: 'dialogue.group_turn_budget', message: '越界', severity: 'error' },
    ]);
    expect(issues).toEqual([
      { field: 'dialogue.voice.voice_id', message: '没填 voice_id' },
      { field: 'dialogue.voice.kind', message: '填了参数却没选类型' },
    ]);
  });

  it('422 上挂的 error.field 也能读出来（复用 connectorErrorField 那条通道）', () => {
    const error = new ApiError(422, 'invalid_request', '音色不行');
    Object.defineProperty(error, 'field', {
      value: 'dialogue.voice.reference_asset_id',
      enumerable: false,
    });
    expect(voiceIssueFromError(error)).toEqual({
      field: 'dialogue.voice.reference_asset_id',
      message: '音色不行',
    });
    expect(voiceIssueFromError(new ApiError(500, 'internal', 'x'))).toBeNull();
  });
});

describe('上传失败：按 error.detail 给人话，且能重试', () => {
  it('413 说清上限是多少 MB', () => {
    const explained = explainUploadFailure(
      new ApiError(413, 'invalid_request', '参考音频超过 10MB 上限', false, 'voice_reference_too_large'),
    );
    expect(explained.reason).toContain(String(MAX_VOICE_BYTES / (1024 * 1024)));
    expect(explained.reason).toContain('MB');
    expect(explained.retriable).toBe(true);
  });

  it('422 invalid_audio 列出支持的格式（不是只说"失败了"）', () => {
    const explained = explainUploadFailure(
      new ApiError(422, 'invalid_request', '不是音频', false, 'voice_reference_invalid_audio'),
    );
    for (const fmt of ['wav', 'mp3', 'flac', 'ogg', 'm4a']) {
      expect(explained.hint).toContain(fmt);
    }
    expect(explained.retriable).toBe(true);
  });

  it('422 empty 说"0 字节"，与"格式不对"分开', () => {
    const empty = explainUploadFailure(
      new ApiError(422, 'invalid_request', '空', false, 'voice_reference_empty'),
    );
    const invalid = explainUploadFailure(
      new ApiError(422, 'invalid_request', '不是音频', false, 'voice_reference_invalid_audio'),
    );
    expect(empty.reason).toContain('空');
    expect(empty.reason).not.toBe(invalid.reason);
  });

  it('403 not_owner / bad_origin 不鼓励无脑重试', () => {
    expect(
      explainUploadFailure(new ApiError(403, 'forbidden', '不是所有者', false, 'not_owner')).retriable,
    ).toBe(false);
    expect(
      explainUploadFailure(new ApiError(403, 'forbidden', '跨站', false, 'bad_origin')).retriable,
    ).toBe(false);
  });

  it('没见过的失败仍然给一句能操作的话（不是空字符串）', () => {
    const explained = explainUploadFailure(new ApiError(500, 'internal', '炸了'));
    expect(explained.reason.length).toBeGreaterThan(0);
    // 兜底分支带回服务端原话，而不是只丢一个"失败"——排查时这句话最有用。
    expect(explained.hint).toContain('炸了');
    expect(explained.retriable).toBe(true);
    // 没有任何 message 时也不能给出空 hint（那会是一个点不明白的按钮）。
    const silent = explainUploadFailure(new ApiError(500, 'internal', ''));
    expect(silent.hint.length).toBeGreaterThan(0);
  });

  it('字节数按人读的单位显示', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2.0 KB');
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.00 MB');
  });
});
