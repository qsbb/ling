/**
 * ⑤ 对话与主动性。表单本体在 PolicyForm，这里只给字段定义。
 *
 * 两组参数：群聊里说多少（group_turn_budget）、什么时候主动找你
 * （proactive_daily_limit / quiet_*）。免打扰用时间选择器——让人手填
 * "1380 分钟"表示 23:00 是不可用的。
 */
import { PolicyForm, type PolicyField } from './PolicyForm';

interface DialoguePageProps {
  onBack: () => void;
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
    key: 'rest_may_skip',
    label: '在休息时可以先不回',
    hint: '关掉之后，即使她在休息也会正常进入判断（由她自己决定回不回）。紧急求助永远不受影响。',
    kind: 'bool',
  },
];

export function DialoguePage({ onBack }: DialoguePageProps) {
  return (
    <PolicyForm
      title="对话与主动性"
      hint="改的是「要不要开口」。保存后会先存成草稿再发布，改错了可以回滚。"
      fields={FIELDS}
      section="dialogue"
      onBack={onBack}
    />
  );
}
