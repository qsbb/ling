/**
 * 测试模式横幅：必须在显著位置说明回复来自确定性 fake provider。
 * 依据 docs/API-WEB-V1.md §7.3。
 */
export function TestModeBanner() {
  return (
    <div className="test-banner" role="status" aria-live="polite">
      <span className="test-banner__dot" aria-hidden="true" />
      <span className="test-banner__text">
        <strong>测试模式</strong>：回复由确定性 fake provider 生成，非真实模型能力
      </span>
    </div>
  );
}
