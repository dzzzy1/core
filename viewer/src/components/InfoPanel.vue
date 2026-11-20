<script setup lang="ts">
import { computed } from "vue";
import type { Picked } from "./SvgPane.vue";
import type { Change, PidObjectInfo } from "@/api";

/** `issue`: the "New issue about <tag>" link (when the plant tracks issues). */
const props = defineProps<{
  picked: Picked | null;
  info?: PidObjectInfo | null;
  loading?: boolean;
  changes?: Change[];
  issue?: { url: string; tag: string } | null;
}>();
defineEmits<{ close: []; select: [id: string] }>();

const subtitle = computed(() => {
  const li = props.info?.list_info;
  if (!li) return "";
  const service = typeof li.service === "string" ? li.service.trim() : "";
  const notes = typeof li.notes === "string" ? li.notes.trim() : "";
  return service || notes;
});

const partsTitle = computed(() => {
  const cat = props.info?.category;
  if (cat === "equipment") return "管口 / 子部件";
  if (cat === "pipe") return "管线内件";
  return "子部件";
});

function formatPartDn(dn: string | null): string {
  if (!dn) return "";
  return dn.toUpperCase().startsWith("DN") ? dn : `DN${dn}`;
}

function dirLabel(dir: "out" | "in" | "peer"): string {
  if (dir === "out") return "出 →";
  if (dir === "in") return "← 入";
  return "↔ 关联";
}
</script>

<template>
  <aside v-if="picked" class="info" aria-live="polite">
    <header>
      <div class="title-block">
        <template v-if="info">
          <div class="title-row">
            <strong class="obj-title">{{ info.tag || info.id }}</strong>
            <span class="cat-badge">{{ info.category_label }}</span>
            <span class="cls-badge">{{ info.dexpi_class }}</span>
          </div>
          <p v-if="subtitle" class="subtitle">{{ subtitle }}</p>
          <div v-if="info.owner" class="owner-row">
            <span class="muted">所属</span>
            <button type="button" class="link-chip" @click="$emit('select', info.owner.id)">
              {{ info.owner.tag || info.owner.id }}
              <span class="chip-sub">{{ info.owner.class }}</span>
            </button>
          </div>
        </template>
        <template v-else>
          <strong>Selected object</strong>
          <span v-if="loading" class="muted loading-hint">加载中…</span>
        </template>
      </div>
      <button class="btn close" type="button" aria-label="Close" @click="$emit('close')">&times;</button>
    </header>

    <template v-if="info">
      <section v-if="info.properties.length" class="section">
        <h3 class="sec-title">工程属性</h3>
        <dl class="prop-grid">
          <template v-for="prop in info.properties" :key="prop.key">
            <dt>{{ prop.label }}</dt>
            <dd :class="{ empty: !prop.value }">{{ prop.value ?? "—" }}</dd>
          </template>
        </dl>
      </section>

      <section v-if="info.connections.length" class="section">
        <h3 class="sec-title">连接关系 <span class="count">{{ info.connections.length }}</span></h3>
        <ul class="conn-list">
          <li v-for="(c, idx) in info.connections" :key="idx" class="conn-item">
            <button
              type="button"
              class="conn-btn"
              :disabled="!c.other_id"
              :title="c.summary"
              @click="c.other_id && $emit('select', c.other_id)"
            >
              <span class="dir-badge" :class="c.direction">{{ dirLabel(c.direction) }}</span>
              <span v-if="c.from_part" class="noz-chip">{{ c.from_part }}</span>
              <span class="conn-target">
                <strong>{{ c.other_tag || c.other_id }}</strong>
                <span v-if="c.other_class" class="conn-cls">{{ c.other_class }}</span>
              </span>
            </button>
            <button
              v-if="c.via_id && c.via_tag"
              type="button"
              class="via-chip"
              :title="`跳转到 ${c.via_tag}`"
              @click.stop="$emit('select', c.via_id)"
            >
              经 {{ c.via_tag }}
            </button>
            <span v-else-if="c.via_tag" class="via-text">{{ c.via_tag }}</span>
          </li>
        </ul>
      </section>

      <section v-if="info.parts.length" class="section">
        <h3 class="sec-title">{{ partsTitle }} <span class="count">{{ info.parts.length }}</span></h3>
        <div class="parts-grid">
          <button
            v-for="part in info.parts"
            :key="part.id"
            type="button"
            class="part-chip"
            :class="{ connected: Boolean(part.connected_to) }"
            :title="part.connected_to ? `${part.tag || part.id} (${part.class}): ${part.connected_to}` : `${part.tag || part.id} (${part.class})`"
            @click="$emit('select', part.id)"
          >
            <span class="part-tag">{{ part.tag || part.id }}</span>
            <span v-if="part.dn" class="part-dn">{{ formatPartDn(part.dn) }}</span>
            <span v-if="part.connected_to" class="part-conn">{{ part.connected_to }}</span>
          </button>
        </div>
      </section>

      <section v-if="info.controls.length" class="section">
        <h3 class="sec-title">控制与联锁 <span class="count">{{ info.controls.length }}</span></h3>
        <ul class="ctrl-list">
          <li v-for="(ctrl, idx) in info.controls" :key="idx" class="ctrl-card" :class="ctrl.kind">
            <div class="ctrl-head">
              <span class="ctrl-badge" :class="ctrl.kind">{{ ctrl.kind === "interlock" ? "联锁" : "回路" }}</span>
              <strong>{{ ctrl.id }}</strong>
            </div>
            <div v-if="ctrl.title" class="ctrl-title">{{ ctrl.title }}</div>
            <div v-if="ctrl.detail" class="ctrl-detail">{{ ctrl.detail }}</div>
          </li>
        </ul>
      </section>

      <div class="meta-foot">
        <code>{{ info.id }}</code>
        <span v-if="info.xml_lines" class="muted">XML L{{ info.xml_lines[0] }}–{{ info.xml_lines[1] }}</span>
      </div>
    </template>

    <dl v-else class="fallback-dl">
      <dt>id</dt>
      <dd><code>{{ picked.id }}</code></dd>
      <template v-if="picked.className">
        <dt>class</dt>
        <dd>{{ picked.className }}</dd>
      </template>
      <template v-if="picked.texts.length">
        <dt>labels</dt>
        <dd>{{ picked.texts.join(" · ") }}</dd>
      </template>
    </dl>

    <a v-if="issue" class="btn issue" :href="issue.url" target="_blank" rel="noopener" data-action="new-issue">New issue about {{ issue.tag }}</a>
    <template v-if="changes && changes.length">
      <strong>Changes</strong>
      <ul class="changes">
        <li v-for="(c, i) in changes" :key="i">
          <span class="badge" :class="c.kind">{{ c.kind }}</span>
          {{ c.object_type }} <code>{{ c.object_id }}</code>
          <span v-if="c.detail" class="muted"> &mdash; {{ c.detail }}</span>
        </li>
      </ul>
    </template>
  </aside>
</template>

<style scoped>
.info {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 3;
  width: min(390px, calc(100% - 16px));
  max-height: calc(100% - 64px);
  overflow: auto;
  padding: var(--space-3);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  font-size: var(--text-sm);
}
header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border);
}
.title-block {
  min-width: 0;
  flex: 1;
}
.title-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.obj-title {
  font-size: var(--text);
  word-break: break-all;
}
.cat-badge,
.cls-badge {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.5;
}
.cat-badge {
  background: var(--accent-soft);
  color: var(--accent);
}
.cls-badge {
  background: var(--bg-soft);
  color: var(--fg-muted);
  border: 1px solid var(--border);
  font-family: var(--mono);
  font-weight: 500;
}
.subtitle {
  margin: 4px 0 0;
  color: var(--fg-muted);
  line-height: 1.35;
}
.owner-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}
.link-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg-soft);
  color: var(--accent);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
}
.link-chip:hover {
  background: var(--accent-soft);
  border-color: var(--accent);
}
.chip-sub {
  color: var(--fg-faint);
  font-size: 10.5px;
}
.loading-hint {
  margin-left: 6px;
  font-size: 11.5px;
}
.close {
  min-height: 24px;
  padding: 0 8px;
  flex-shrink: 0;
}
.section {
  margin-bottom: var(--space-3);
}
.sec-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 6px;
  font-size: 11.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.02em;
  color: var(--fg-muted);
}
.count {
  padding: 0 5px;
  border-radius: 99px;
  background: var(--bg-sunk);
  color: var(--fg-muted);
  font-size: 10.5px;
  font-weight: 600;
}
.prop-grid,
.fallback-dl {
  display: grid;
  grid-template-columns: minmax(76px, auto) 1fr;
  gap: 4px var(--space-3);
  margin: 0;
}
.prop-grid dt,
.fallback-dl dt {
  color: var(--fg-muted);
  white-space: nowrap;
}
.prop-grid dd,
.fallback-dl dd {
  margin: 0;
  word-break: break-word;
}
.prop-grid dd.empty {
  color: var(--fg-faint);
}
.conn-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 5px;
}
.conn-item {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-soft);
}
.conn-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: 1;
  min-width: 0;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.conn-btn:hover strong {
  color: var(--accent);
  text-decoration: underline;
}
.conn-btn:disabled {
  cursor: default;
}
.dir-badge {
  flex-shrink: 0;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 10.5px;
  font-weight: 600;
}
.dir-badge.out {
  background: var(--ok-soft);
  color: var(--ok);
}
.dir-badge.in {
  background: var(--info-soft);
  color: var(--info);
}
.dir-badge.peer {
  background: var(--na-soft);
  color: var(--na);
}
.noz-chip {
  flex-shrink: 0;
  padding: 0 5px;
  border-radius: 3px;
  background: var(--surface);
  border: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 11px;
  color: var(--fg);
}
.conn-target {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px;
  min-width: 0;
  word-break: break-all;
}
.conn-cls {
  color: var(--fg-faint);
  font-size: 11px;
}
.via-chip {
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--fg-muted);
  font-family: var(--mono);
  font-size: 10.5px;
  cursor: pointer;
}
.via-chip:hover {
  color: var(--accent);
  border-color: var(--accent);
  background: var(--accent-soft);
}
.via-text {
  color: var(--fg-faint);
  font-size: 10.5px;
}
.parts-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.part-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  padding: 3px 7px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg-soft);
  color: var(--fg);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
}
.part-chip.connected {
  border-color: var(--accent);
  background: var(--surface);
}
.part-chip:hover {
  background: var(--accent-soft);
  border-color: var(--accent);
}
.part-tag {
  font-weight: 600;
  font-family: var(--mono);
}
.part-dn {
  color: var(--fg-muted);
  font-size: 10.5px;
}
.part-conn {
  color: var(--fg-muted);
  font-size: 10.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 170px;
}
.ctrl-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 6px;
}
.ctrl-card {
  padding: 6px 8px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: var(--bg-soft);
}
.ctrl-card.interlock {
  border-left: 3px solid var(--bad);
}
.ctrl-card.loop {
  border-left: 3px solid var(--info);
}
.ctrl-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ctrl-badge {
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 10.5px;
  font-weight: 600;
}
.ctrl-badge.interlock {
  background: var(--bad-soft);
  color: var(--bad);
}
.ctrl-badge.loop {
  background: var(--info-soft);
  color: var(--info);
}
.ctrl-title {
  margin-top: 2px;
  font-weight: 500;
}
.ctrl-detail {
  margin-top: 2px;
  color: var(--fg-muted);
  font-size: 11.5px;
  line-height: 1.35;
}
.meta-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
  font-size: 11px;
  color: var(--fg-muted);
  margin-bottom: var(--space-2);
}
.issue {
  display: inline-flex;
  margin-bottom: var(--space-2);
}
.issue:hover {
  text-decoration: none;
}
.changes {
  margin: var(--space-1) 0 0;
  padding-left: 0;
  list-style: none;
  display: grid;
  gap: 4px;
}
</style>
