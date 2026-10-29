/**
 * ④ 记忆与学习。表单本体在 PolicyForm，这里只给字段定义。
 *
 * 四个参数原来是代码里的常量（RELATIVE_SCORE_FLOOR 等）。抽出来是因为
 * 它们是用户真正会想调的东西：觉得角色记性太好/太差、翻旧账太频繁，
 * 都该在这里改，而不是改代码重部署。
 */
import { PolicyForm, type PolicyField } from './PolicyForm';

interface MemoryPageProps {
  onBack: () => void;
}

const FIELDS: PolicyField[] = [
  {
    key: 'relative_score_floor',
    label: '召回相关性下限',
    hint: '只保留与最相关那条同量级的结果。调高 = 只想起最相关的，调低 = 更容易联想。',
    step: 'any',
    min: 0,
    max: 1,
  },
  {
    key: 'rerank_top_n',
    label: '精排头部条数',
    hint: '重排只看前 N 条。调高 = 更准但更慢更贵。',
    step: 1,
    min: 1,
    max: 100,
  },
  {
    key: 'recency_half_life_days',
    label: '时间衰减半衰期（天）',
    hint: '越小越偏向近期的事。调大 = 更愿意提旧事。',
    step: 'any',
    min: 1,
    max: 3650,
  },
  {
    key: 'max_retention_days',
    label: '最长保留天数',
    hint: '超过这个天数的记忆不会再被召回。',
    step: 1,
    min: 1,
    max: 3650,
  },
];

export function MemoryPage({ onBack }: MemoryPageProps) {
  return (
    <PolicyForm
      title="记忆与学习"
      hint="改的是「它记住/学会什么」。保存后会先存成草稿再发布，改错了可以回滚。"
      fields={FIELDS}
      section="memory"
      onBack={onBack}
    />
  );
}
