/**
 * Lines an extraction set aside instead of putting them in the draft. They are shown for the reviewer to read;
 * none of them is written to the knowledge base. Pure functions only: the page draws what these return.
 */

/** Why a line was set aside, in the reviewer's words. Codes come from the backend. */
export const HELD_REASONS = {
  number_not_in_quote: {label: '数值不在引用的原文里',
    hint: '这条内容里的数值，在它引用的那段原文中找不到。可能是提取错了，也可能只是引用的位置没有覆盖到。'},
  quote_names_another_sample: {label: '引用的原文说的是别的配方',
    hint: '引用的原文里出现了其他配方的名字，却没有出现这条内容所属的配方，数据可能挂错了对象。'},
  quote_not_found: {label: '引用的原文找不到', hint: '这条内容标注的引文，在原文的对应位置找不到。'},
  entity_not_kept: {label: '所属的实体没有保留', hint: '这条内容所属的实体没有进入提取结果，所以它没有可以挂靠的对象。'},
  entity_unplaced: {label: '在原文中没有出处', hint: '这个名称在原文里找不到可以定位的位置。'},
  mention_not_found: {label: '在原文中没有出处', hint: '这个名称标注的出处，在原文的对应位置找不到。'},
  link_ends_invalid: {label: '关联的两端不成立', hint: '这条关联的两端不是提取结果里两个不同的实体。'},
  table_value_unattached: {label: '表格数值没有对应的配方', hint: '程序从表格里读出了这个数值，但没能确定它属于哪个配方。'},
};

/** The wording for one set-aside line; a reason the page has no wording for is shown as the backend wrote it. */
export function heldReason(item) {
  return HELD_REASONS[item?.reason_code] || {label: item?.reason || '未说明原因', hint: ''};
}

/** The notice above the draft; empty when nothing was set aside. */
export function heldNotice(held) {
  const total = held?.total || 0;
  return total ? `另有 ${total} 条提取内容没有通过程序核对，不会写入知识库。` : '';
}

/** Set-aside lines of one draft entity. */
export function heldOf(held, entityKey) {
  return (held?.items || []).filter(item => item.entity_key && item.entity_key === entityKey);
}

/** All set-aside lines grouped by reason, the largest group first; lines keep their reading order inside a group. */
export function heldGroups(items) {
  const groups = new Map();
  for (const item of items || []) {
    const {label, hint} = heldReason(item);
    if (!groups.has(label)) groups.set(label, {label, hint, items: []});
    groups.get(label).items.push(item);
  }
  return [...groups.values()].sort((a, b) => b.items.length - a.items.length);
}

/** What the line is about, for a card that stands outside its entity's page. */
export function heldSubject(item) {
  return [item?.entity_name, item?.section].filter(Boolean).join(' · ');
}

/** The values the cited passage does not show; empty when the reason is not about values. */
export function heldMissing(item) {
  return item?.numbers?.length ? `引用的原文里没有：${item.numbers.join('、')}` : '';
}
