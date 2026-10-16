/**
 * 与 JSONReader 第 6 步一致：外键始终展示，其余 composite_score >= 阈值才展示。
 * 比较是大于等于，不是严格大于。
 */
export function shownAtCqlThreshold(
  discoveryMethod: string,
  compositeScore: number | null,
  threshold: number,
): boolean {
  if (discoveryMethod === 'foreign_key_constraint') return true
  return typeof compositeScore === 'number' && compositeScore >= threshold
}
