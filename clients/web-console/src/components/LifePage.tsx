/**
 * ⑧ 生活与活动。表单本体在 PolicyForm，这里只给字段定义。
 *
 * 这些参数原来散在 presence / agenda 两个域的模块级常量里。抽出来是因为
 * 它们直接决定「像不像人」：精力掉太快显得虚弱，社交负担恢复太慢会
 * 一直不想说话。
 *
 * **作息（几点醒/几点睡）不在这里**：它按 actor 存在 actor_routines 表，
 * 再放一份到配置就是第二份权威，两边不一致时没人说得清以谁为准。
 */
import { PolicyForm, type PolicyField } from './PolicyForm';

interface LifePageProps {
  onBack: () => void;
}

const FIELDS: PolicyField[] = [
  {
    key: 'scene_ttl_minutes',
    label: '场景有效期（分钟）',
    hint: '「此刻在做什么」能挂多久。过了就回到未知，不再自述。',
    step: 1,
    min: 1,
    max: 1440,
  },
  {
    key: 'energy_decay_per_hour',
    label: '精力每小时消耗',
    hint: '0-100 量表。调大 = 更容易累。',
    step: 'any',
    min: 0,
    max: 100,
  },
  {
    key: 'social_load_per_turn',
    label: '每轮对话的社交负担',
    hint: '聊得越多越需要独处。调大 = 更容易"聊累了"。',
    step: 'any',
    min: 0,
    max: 100,
  },
  {
    key: 'social_load_decay_per_hour',
    label: '社交负担每小时恢复',
    hint: '独处多久能缓过来。调小 = 恢复更慢。',
    step: 'any',
    min: 0,
    max: 100,
  },
  {
    key: 'max_live_plans',
    label: '同时最多几件小事',
    hint: '排太多会让"在忙"变成常态。',
    step: 1,
    min: 1,
    max: 20,
  },
];

export function LifePage({ onBack }: LifePageProps) {
  return (
    <PolicyForm
      title="生活与活动"
      hint="改的是「它自己在干嘛」。保存后会先存成草稿再发布，改错了可以回滚。作息不在这里——它按角色单独存。"
      fields={FIELDS}
      section="life"
      onBack={onBack}
    />
  );
}
