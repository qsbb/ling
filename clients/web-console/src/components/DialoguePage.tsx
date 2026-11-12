/**
 * ⑤ 对话与主动性。表单本体在 PolicyForm，这里只给字段定义 + 语音设置面板。
 *
 * 三组参数：
 *
 * - **对话节奏**（`PolicyForm` 的数字字段）：群聊里说多少、什么时候主动找你、
 *   免打扰时段。免打扰用时间选择器——让人手填"1380 分钟"表示 23:00 是不可用的。
 * - **意愿档位**（`VoiceSettings`）：她愿不愿意开口说话。三档语义，不是概率。
 * - **音色**（`VoiceSettings`）：三家各管各的字段，MiMo 要上传参考音频。
 *
 * ## 为什么这一页自己再读一次 `/v1/settings/config`
 *
 * `PolicyForm` 只从 `dialogue` 段取**数字**（它的 `Values` 是 `Record<string, number>`），
 * `voice_willingness` 是枚举、`voice` 是对象，塞不进去。与其把 `PolicyForm` 的
 * 类型改成 `unknown` 的袋子（那样每个数字字段都要自己收敛类型），不如让面板
 * 自己拿一份结构化数据：
 *
 * - 两者读的是**同一个**端点，所以不会出现"数字来自 A、音色来自 B"的漂移；
 * - 保存仍只发**一次** `PUT /v1/settings/draft`——由 `PolicyForm` 发，`voice`
 *   与 `voice_willingness` 通过 `beforeSave` 那一刻的闭包并进去。
 *
 * 本次只读不改 `PolicyForm` 的接口之外的东西（它新增的是可选 props）。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type SetStateAction } from 'react';
import { api } from '../api';
import { explainError, type ExplainedError } from '../errors';
import {
  blockingSaveReason,
  normalizeWillingness,
  voiceDraftFrom,
  voiceIssuesFromDraft,
  voicePayloadFrom,
  voiceIssueFromError,
  type VoiceDraft,
  type VoiceField,
} from '../voiceSettings';
import type { VoiceWillingness } from '../types';
import { PolicyForm, fieldKeyWithinSection, type PolicyField } from './PolicyForm';
import { VoiceSettings } from './VoiceSettings';

interface DialoguePageProps {
  onBack: () => void;
  /**
   * 返回按钮的文字。默认「返回设置」是 PolicyForm 的历史默认值；Step 4 之后
   * 本页挂在 `/persona?tab=dialogue` 里，宿主会传「返回角色设定」。
   */
  backLabel?: string;
}

const FIELDS: PolicyField[] = [
  {
    key: 'group_turn_budget',
    label: '群聊发言预算',
    hint: '近期已在群里说了几次就不再接话。调大 = 更爱说；被点名时不受这条限制。',
    step: 1,
    min: 1,
    max: 20,
  },
  {
    key: 'proactive_daily_limit',
    label: '每天最多主动联系',
    hint: '0 表示完全不主动找你。调大会变成骚扰。',
    step: 1,
    min: 0,
    max: 20,
  },
  {
    key: 'quiet_start_minute',
    label: '免打扰开始',
    hint: '按你所在时区计算。跨午夜是常态，不需要开始早于结束。',
    kind: 'minutes',
  },
  {
    key: 'quiet_end_minute',
    label: '免打扰结束',
    hint: '这段时间里它不会主动发起对话；你主动找它仍然会回。',
    kind: 'minutes',
  },
  {
    key: 'private_may_stay_silent',
    label: '私聊里可以不回',
    hint: '真人聊天不是句句有回应。打开后，私聊也可能选择不回——消息仍然会被她看到并记住。',
    kind: 'bool',
  },
  {
    key: 'group_mention_requires_reply',
    label: '群里被 @ 必须回',
    hint: '关掉之后，@ 只是让她更倾向回应，不强制。注意：关掉后你 @ 她也可能不理你。',
    kind: 'bool',
  },
  {
    key: 'proactive_delivery_enabled',
    label: '真的主动联系我',
    hint: '关着时只生成她会说的话（在「它现在在干嘛」页能看到），不会真的发出去。开启前建议先看几天草稿确认语气。受勿扰时段与每日上限约束。',
    kind: 'bool',
  },
  {
    key: 'group_reply_delivery_enabled',
    label: '在群里真的发送回复',
    hint: '关着时她只判断「这条要不要回」，不会真的在群里开口——群里看不到任何回复。打开后，决定要回就会真的生成并发送，发不出去会记成回复失败。群里说错话比不说更难收拾，所以默认关，由你决定什么时候打开。',
    kind: 'bool',
  },
  {
    key: 'tools_enabled',
    label: '允许对话调用工具',
    hint: '开启后，已声明支持工具调用的对话模型可以查询当前时间和本轮可读的本地资料。',
    kind: 'bool',
  },
  {
    key: 'rest_may_skip',
    label: '在休息时可以先不回',
    hint: '关掉之后，即使她在休息也会正常进入判断（由她自己决定回不回）。紧急求助永远不受影响。',
    kind: 'bool',
  },
];

/**
 * 语音那一段的本地状态。
 *
 * `loaded` 为 false 时**不渲染面板**：否则会先画一个"没有音色"的空表单，
 * 一帧之后才变成已保存的那个音色——用户会以为自己的配置丢了。
 */
interface VoiceState {
  loaded: boolean;
  willingness: VoiceWillingness;
  draft: VoiceDraft;
  error: ExplainedError | null;
}

export function DialoguePage({ onBack, backLabel }: DialoguePageProps) {
  const [voice, setVoice] = useState<VoiceState>({
    loaded: false,
    // 默认 `natural`：与后端 `DialoguePolicy.voice_willingness` 的默认值一致。
    willingness: 'natural',
    draft: voiceDraftFrom(null),
    error: null,
  });
  const [uploading, setUploading] = useState(false);
  /** 字段级错误（音色）：键是短名。 */
  const [fieldIssues, setFieldIssues] = useState<ReadonlyMap<VoiceField | 'kind', string>>(
    new Map(),
  );

  const loadVoice = useCallback(async () => {
    try {
      const config = await api.settingsConfig();
      const dialogue = config.dialogue ?? {};
      setVoice({
        loaded: true,
        willingness: normalizeWillingness(dialogue.voice_willingness),
        draft: voiceDraftFrom(dialogue.voice),
        error: null,
      });
      setFieldIssues(new Map());
    } catch (err) {
      // 读不到就把话说出来，且**不假装默认值就是你存的值**。
      setVoice((current) => ({ ...current, loaded: true, error: explainError(err) }));
    }
  }, []);

  useEffect(() => {
    void loadVoice();
  }, [loadVoice]);

  const setDraft = useCallback((next: SetStateAction<VoiceDraft>) => {
    setVoice((current) => ({
      ...current,
      draft: typeof next === 'function' ? next(current.draft) : next,
    }));
  }, []);

  /**
   * 面板的值 → 请求体片段。**点保存那一刻读当前值**，不是读闭包里的旧值。
   *
   * 为什么用 ref：`sections` 是渲染函数，`PolicyForm` 保存时调的是它拿到的
   * `beforeSave`。如果这里把 `voice` 直接闭包进去，只要某次渲染没重新创建
   * `beforeSave`，存下去的就是上一帧的音色——界面显示 A、库里存 B，
   * 而且两边都不报错。ref 每帧同步，从不落后。
   */
  const voiceRef = useRef(voice);
  voiceRef.current = voice;

  /**
   * 保存前拦截：缺 `voice_id` / 缺参考音频 / 上传中。
   *
   * **它不复制后端的校验规则**：后端 `ports/config.py::voice_requirement_issue()`
   * 仍是唯一判定。这里只拦"发出去也注定被拒、且用户当场就能改"的情况，
   * 让提示在点保存时就出现。
   */
  const beforeSave = useCallback(() => {
    if (uploading) {
      return {
        field: 'dialogue.voice.reference_asset_id',
        message: '参考音频还在上传，等它传完再保存（否则存下去的音色没有素材）。',
      };
    }
    return blockingSaveReason(voiceRef.current.draft);
  }, [uploading]);

  /**
   * 草稿返回的 `validation_errors` → 音色字段的错误。
   *
   * **这是音色校验的实际通道**：`settings/service._voice_issues` 的结果进的是
   * `DraftResult.validation_errors`（HTTP 仍是 200），**不是** 422。所以"按
   * `error.field` 标到输入框"必须在这里读一次，只读 422 的 `error.field` 会漏掉
   * 绝大多数音色错误。
   *
   * 非音色的 field（属于数值字段的）不在这里处理——`PolicyForm` 已经逐条标过。
   */
  const collectDraftIssues = useCallback((issues: readonly { field: string; message: string; severity?: string }[]) => {
    const next = new Map<VoiceField | 'kind', string>();
    for (const issue of voiceIssuesFromDraft(issues)) {
      const short = fieldKeyWithinSection(issue.field, 'dialogue.voice');
      if (short === 'kind' || short === 'voice_id' || short === 'reference_asset_id' ||
          short === 'speed' || short === 'vol' || short === 'pitch' || short === 'emotion') {
        next.set(short, issue.message);
      }
    }
    setFieldIssues(next);
  }, []);

  /** `PUT` 失败时挂在 `ApiError` 上的 `error.field`（422/409）也一样要标上去。 */
  const collectErrorIssue = useCallback((error: unknown) => {
    const issue = voiceIssueFromError(error);
    if (issue === null) return;
    const short = fieldKeyWithinSection(issue.field, 'dialogue.voice');
    if (short === 'kind' || short === 'voice_id' || short === 'reference_asset_id' ||
        short === 'speed' || short === 'vol' || short === 'pitch' || short === 'emotion') {
      setFieldIssues(new Map([[short, issue.message]]));
    }
  }, []);

  /**
   * 交给 `PolicyForm` 的附加请求体：`voice` 与 `voice_willingness`。
   *
   * **这是"值真的送到后端"的那一处接线**：`PolicyForm` 每次保存前调它，
   * 结果并进同一次 `PUT /v1/settings/draft`。
   */
  const extraDialogue = useCallback(() => {
    const current = voiceRef.current;
    if (!current.loaded) return {};
    return {
      voice: voicePayloadFrom(current.draft),
      voice_willingness: current.willingness,
    };
  }, []);

  const issuesForPanel = useMemo(() => new Map<string, string>(fieldIssues), [fieldIssues]);

  return (
    <PolicyForm
      title="对话与主动性"
      hint="改的是「要不要开口」。保存后会先存成草稿再发布，改错了可以回滚。"
      fields={FIELDS}
      section="dialogue"
      onBack={onBack}
      {...(backLabel ? { backLabel } : {})}
      // 语音的字段要不要进请求体，由这里决定；它必须看见面板的最新值。
      beforeSave={beforeSave}
      extraValues={extraDialogue}
      onDraftIssues={collectDraftIssues}
      onSaveError={collectErrorIssue}
      busy={uploading}
      sections={({ saving, issues }) => {
        // 把 PolicyForm 收敛过的短名错误并进面板：`dialogue.voice.voice_id`
        // 已经被 `fieldKeyWithinSection` 去掉 `dialogue.` → `voice.voice_id`。
        const merged = new Map<VoiceField | 'kind', string>();
        for (const [key, value] of [...issuesForPanel, ...issues]) {
          const short = fieldKeyWithinSection(key, 'voice');
          if (short === 'kind' || short === 'voice_id' || short === 'reference_asset_id' ||
              short === 'speed' || short === 'vol' || short === 'pitch' || short === 'emotion') {
            merged.set(short, value);
          }
        }
        if (!voice.loaded) {
          return <p className="page__hint">正在读取语音设置…</p>;
        }
        if (voice.error) {
          return (
            <div className="banner banner--error" role="status">
              <div>
                <strong>没能读取语音设置</strong>
                <div className="banner__detail">
                  {voice.error.reason}。下面的音色是默认值，不是你之前存的那份。
                </div>
              </div>
              <button type="button" className="button button--small" onClick={() => void loadVoice()}>
                重试
              </button>
            </div>
          );
        }
        return (
          <VoiceSettings
            willingness={voice.willingness}
            onWillingness={(next) => setVoice((current) => ({ ...current, willingness: next }))}
            draft={voice.draft}
            onDraft={setDraft}
            issues={merged}
            saving={saving}
            onUploadingChange={setUploading}
          />
        );
      }}
    />
  );
}
