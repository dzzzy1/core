<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";
import type { SanitizedSvg } from "@/lib/sanitize";
import { fitView, transformOf, zoomAt, type View } from "@/lib/panzoom";
import { cssString } from "@/lib/highlight";

export interface Layer {
  /** DOM id of the layer; highlight CSS is scoped by it. Unique in the page. */
  id: string;
  svg: SanitizedSvg | null;
  opacity?: number;
}

export interface Picked {
  id: string;
  layer: string;
  className: string;
  texts: string[];
}

const props = withDefaults(
  defineProps<{
    layers: Layer[];
    view: View;
    /** Extra CSS (highlights, selection), already scoped to the layer ids. */
    css?: string;
    autoFit?: boolean;
    label?: string;
    emptyText?: string;
    /**
     * `@pick`: an object was picked (null: none). Declared as a prop so the pane knows whether
     * anyone listens: only then do clicks and the keys N, P and Enter pick objects.
     */
    onPick?: (picked: Picked | null) => void;
  }>(),
  { css: "", autoFit: true, label: "", emptyText: "Nothing to draw." },
);
const pickable = computed(() => Boolean(props.onPick));
const emitPick = (picked: Picked | null) => props.onPick?.(picked);

const container = ref<HTMLDivElement | null>(null);
const hosts = new Map<string, HTMLElement>();
const styleEl = document.createElement("style");

const size = computed(() => {
  const first = props.layers.find((l) => l.svg);
  return { width: first?.svg?.width || 1000, height: first?.svg?.height || 700 };
});
const hasDrawing = computed(() => props.layers.some((l) => l.svg));

function setHost(id: string, el: unknown): void {
  if (el instanceof HTMLElement) hosts.set(id, el);
  else hosts.delete(id);
}

/** Put each layer's cleaned SVG element into its host (DOM import, no HTML parsing). */
function mount(): void {
  for (const layer of props.layers) {
    const host = hosts.get(layer.id);
    if (!host) continue;
    if (layer.svg) host.replaceChildren(document.importNode(layer.svg.element, true));
    else host.replaceChildren();
  }
}

function fit(): void {
  const el = container.value;
  if (!el) return;
  fitView(props.view, el.clientWidth, el.clientHeight, size.value.width, size.value.height);
}

function zoomBy(factor: number): void {
  const el = container.value;
  if (!el) return;
  zoomAt(props.view, factor, el.clientWidth / 2, el.clientHeight / 2);
}

function actualSize(): void {
  const el = container.value;
  if (!el) return;
  // 1 user unit (mm on a DEXPI sheet) ~ 3.78 CSS px at 96 dpi.
  const k = 96 / 25.4;
  zoomAt(props.view, k / props.view.k, el.clientWidth / 2, el.clientHeight / 2);
}

/**
 * Centre the object with DOM id `objectId` (zooming in if the sheet is shown very small), as drawn
 * in layer `layerId` or else in the first layer that has it.
 */
function focus(objectId: string, layerId?: string): boolean {
  const el = container.value;
  const stage = el?.querySelector<HTMLElement>(".stage");
  if (!el || !stage) return false;
  const candidates = layerId && hosts.has(layerId) ? [hosts.get(layerId)!] : [...hosts.values()];
  for (const host of candidates) {
    const target = host.querySelector(`[id="${cssString(objectId)}"]`);
    if (!target) continue;
    const box = target.getBoundingClientRect();
    if (box.width === 0 && box.height === 0) return false;
    const origin = stage.getBoundingClientRect();
    const k = props.view.k;
    const cx = (box.left + box.width / 2 - origin.left) / k;
    const cy = (box.top + box.height / 2 - origin.top) / k;
    if (cx < -50 || cy < -50 || cx > size.value.width + 50 || cy > size.value.height + 50) return false;
    const span = Math.max(box.width / k, box.height / k, 8);
    const wanted = Math.min(el.clientWidth, el.clientHeight) / (span * 4);
    if (wanted > k) props.view.k = Math.min(wanted, 16);
    props.view.x = el.clientWidth / 2 - cx * props.view.k;
    props.view.y = el.clientHeight / 2 - cy * props.view.k;
    return true;
  }
  return false;
}

/**
 * Fallback focus when a picked object (such as an internal virtual off-page connector) has no
 * drawn SVG strokes of its own, panning smoothly to its sheet coordinates `(cx, cy)` in mm.
 */
function focusPoint(cx: number, cy: number): boolean {
  const el = container.value;
  if (!el) return false;
  if (cx < -50 || cy < -50 || cx > size.value.width + 50 || cy > size.value.height + 50) return false;
  const wanted = Math.min(el.clientWidth, el.clientHeight) / (24 * 4);
  if (wanted > props.view.k) props.view.k = Math.min(wanted, 8);
  props.view.x = el.clientWidth / 2 - cx * props.view.k;
  props.view.y = el.clientHeight / 2 - cy * props.view.k;
  return true;
}

defineExpose({ fit, zoomBy, focus, focusPoint });

watch(
  () => props.layers.map((l) => [l.id, l.svg] as const),
  async (current, previous) => {
    await nextTick();
    mount();
    const hadDrawing = previous?.some(([, svg]) => svg);
    if (props.autoFit && !hadDrawing && current.some(([, svg]) => svg)) fit();
  },
  { immediate: true, flush: "post" },
);

// The object the keys N and P stepped to (its DOM id), marked in the drawing until picked or left.
const cursor = ref<string | null>(null);
const announcement = ref("");
watch(
  () => [props.css, cursor.value] as const,
  ([css, id]) => {
    const top = props.layers[props.layers.length - 1];
    const mark = id && top ? `[id="${cssString(top.id)}"] [id="${cssString(id)}"]{filter:drop-shadow(0 0 1.5px var(--accent)) drop-shadow(0 0 1.5px var(--accent))}` : "";
    styleEl.textContent = [css, mark].filter(Boolean).join("\n");
  },
  { immediate: true },
);
watch(
  () => props.layers.map((l) => l.svg),
  () => {
    cursor.value = null;
    announcement.value = "";
  },
);

onMounted(() => {
  container.value?.appendChild(styleEl);
  mount();
});
onBeforeUnmount(() => styleEl.remove());

// Pointer handling: drag pans, a press without movement picks, two pointers pinch-zoom.
const pointers = new Map<number, { x: number; y: number }>();
let start: { x: number; y: number; clientX: number; clientY: number; target: EventTarget | null } | null = null;
let moved = false;
let pinch: { distance: number } | null = null;
const dragging = ref(false);

function local(event: PointerEvent | WheelEvent): { x: number; y: number } {
  const rect = container.value?.getBoundingClientRect();
  return { x: event.clientX - (rect?.left ?? 0), y: event.clientY - (rect?.top ?? 0) };
}

function onPointerDown(event: PointerEvent): void {
  if (event.button !== 0 && event.pointerType === "mouse") return;
  if ((event.target as Element | null)?.closest?.(".controls")) return;
  container.value?.setPointerCapture(event.pointerId);
  pointers.set(event.pointerId, local(event));
  if (pointers.size === 1) {
    start = { ...local(event), clientX: event.clientX, clientY: event.clientY, target: event.target };
    moved = false;
  } else if (pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    pinch = { distance: Math.hypot((a?.x ?? 0) - (b?.x ?? 0), (a?.y ?? 0) - (b?.y ?? 0)) };
    moved = true;
  }
}

function onPointerMove(event: PointerEvent): void {
  const previous = pointers.get(event.pointerId);
  if (!previous) return;
  const point = local(event);
  pointers.set(event.pointerId, point);
  if (pointers.size >= 2 && pinch) {
    const [a, b] = [...pointers.values()];
    if (!a || !b) return;
    const distance = Math.hypot(a.x - b.x, a.y - b.y);
    if (pinch.distance > 0) zoomAt(props.view, distance / pinch.distance, (a.x + b.x) / 2, (a.y + b.y) / 2);
    pinch.distance = distance;
    return;
  }
  if (start && !moved && Math.hypot(point.x - start.x, point.y - start.y) > 4) moved = true;
  if (moved) {
    dragging.value = true;
    props.view.x += point.x - previous.x;
    props.view.y += point.y - previous.y;
  }
}

function onPointerUp(event: PointerEvent): void {
  if (!pointers.has(event.pointerId)) return;
  pointers.delete(event.pointerId);
  if (pointers.size < 2) pinch = null;
  if (pointers.size === 0) {
    if (start && !moved) pick(start.target, start.clientX, start.clientY);
    start = null;
    dragging.value = false;
  }
}

function onWheel(event: WheelEvent): void {
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;
  const factor = Math.exp(-event.deltaY * unit * 0.0015);
  const { x, y } = local(event);
  zoomAt(props.view, factor, x, y);
}

function onKey(event: KeyboardEvent): void {
  // Browser and system shortcuts (Ctrl/Cmd +, Ctrl+N) pass; Enter and Space on a zoom button press it.
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if ((event.key === "Enter" || event.key === " ") && event.target !== container.value) return;
  const step = 40;
  const actions: Record<string, () => void> = {
    "+": () => zoomBy(1.25),
    "=": () => zoomBy(1.25),
    "-": () => zoomBy(0.8),
    "0": fit,
    ArrowLeft: () => (props.view.x += step),
    ArrowRight: () => (props.view.x -= step),
    ArrowUp: () => (props.view.y += step),
    ArrowDown: () => (props.view.y -= step),
    Escape: () => {
      cursor.value = null;
      emitPick(null);
    },
  };
  if (pickable.value) {
    actions.n = () => stepObject(1);
    actions.N = () => stepObject(-1);
    actions.p = () => stepObject(-1);
    actions.P = () => stepObject(-1);
    actions.Enter = pickCursor;
    actions[" "] = pickCursor;
  }
  const action = actions[event.key];
  if (action) {
    event.preventDefault();
    action();
  }
}

function pickedOf(group: Element, layer: string): Picked {
  const texts = [...new Set(Array.from(group.querySelectorAll("text"), (t) => (t.textContent ?? "").trim()).filter(Boolean))];
  return { id: group.id, layer, className: group.getAttribute("class") ?? "", texts: texts.slice(0, 20) };
}

function isIgnoredGroup(el: Element | null): boolean {
  if (!el) return true;
  const id = el.id;
  return !id || id === "SheetArtwork1" || id === "SheetBorder1" || id.startsWith("diagram-");
}

function pointToSegmentDist(px: number, py: number, x1: number, y1: number, x2: number, y2: number): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  if (lenSq === 0) return Math.hypot(px - x1, py - y1);
  const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lenSq));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

/** Check whether `(cx, cy)` is within `tol` CSS pixels of any direct `<line>`/`<polyline>` segment of `group`. */
function nearOwnWire(group: Element, cx: number, cy: number, tol: number): boolean {
  const wires: Element[] = [];
  for (const child of Array.from(group.children)) {
    const tag = child.tagName.toLowerCase();
    if (tag === "line" || tag === "polyline") wires.push(child);
    else if (tag === "text") {
      const r = child.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && cx >= r.left - tol && cx <= r.right + tol && cy >= r.top - tol && cy <= r.bottom + tol) {
        return true;
      }
    }
  }
  if (!wires.length) return false;
  for (const wire of wires) {
    const gfx = wire as SVGGraphicsElement;
    const ctm = typeof gfx.getScreenCTM === "function" ? gfx.getScreenCTM() : null;
    if (!ctm) {
      const r = wire.getBoundingClientRect();
      if (cx >= r.left - tol && cx <= r.right + tol && cy >= r.top - tol && cy <= r.bottom + tol) return true;
      continue;
    }
    const project = (x: number, y: number): [number, number] => [ctm.a * x + ctm.c * y + ctm.e, ctm.b * x + ctm.d * y + ctm.f];
    if (wire.tagName.toLowerCase() === "line") {
      const x1 = Number(wire.getAttribute("x1") ?? 0);
      const y1 = Number(wire.getAttribute("y1") ?? 0);
      const x2 = Number(wire.getAttribute("x2") ?? 0);
      const y2 = Number(wire.getAttribute("y2") ?? 0);
      const [sx1, sy1] = project(x1, y1);
      const [sx2, sy2] = project(x2, y2);
      if (pointToSegmentDist(cx, cy, sx1, sy1, sx2, sy2) <= tol) return true;
    } else {
      const raw = (wire.getAttribute("points") ?? "").trim();
      const nums = raw.split(/[\s,]+/).map(Number).filter((n) => !Number.isNaN(n));
      for (let i = 0; i + 3 < nums.length; i += 2) {
        const [sx1, sy1] = project(nums[i]!, nums[i + 1]!);
        const [sx2, sy2] = project(nums[i + 2]!, nums[i + 3]!);
        if (pointToSegmentDist(cx, cy, sx1, sy1, sx2, sy2) <= tol) return true;
      }
    }
  }
  return false;
}

function domDepth(el: Element): number {
  let depth = 0;
  let cur: Element | null = el;
  while (cur) {
    depth += 1;
    cur = cur.parentElement;
  }
  return depth;
}

/**
 * Find the smallest-area `g.representation-group[id]` inside `root` whose padded screen bounding
 * box (`pad` px on each side) contains `(cx, cy)`. Makes hollow symbols (valves, bubbles,
 * reducers, flanges) and thin 0.2 mm lines effortless to click without falsely matching huge
 * L-shaped pipe containers on empty whitespace.
 */
function findSmallestMatchingGroup(root: Element, cx: number, cy: number, pad: number): Element | null {
  let best: Element | null = null;
  let bestArea = Number.POSITIVE_INFINITY;
  let bestDepth = -1;
  const candidates = root.querySelectorAll("g.representation-group[id]");
  for (const cand of Array.from(candidates)) {
    if (isIgnoredGroup(cand)) continue;
    const rect = cand.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    if (cx < rect.left - pad || cx > rect.right + pad || cy < rect.top - pad || cy > rect.bottom + pad) continue;
    // For large wire-only groups (e.g. L-shaped pipe runs or PipingNetworkSystem containers),
    // require the click to actually lie near one of the group's own wire segments so clicking
    // blank space inside an L-bend does not capture the pipe.
    if (rect.width > 44 && rect.height > 44) {
      let hasSolidBody = false;
      let hasWire = false;
      for (const ch of Array.from(cand.children)) {
        const tag = ch.tagName.toLowerCase();
        if (tag === "rect" || tag === "circle" || tag === "ellipse" || tag === "polygon" || tag === "path") {
          hasSolidBody = true;
          break;
        }
        if (tag === "line" || tag === "polyline") hasWire = true;
      }
      if (!hasSolidBody) {
        if (hasWire) {
          if (!nearOwnWire(cand, cx, cy, pad + 2)) continue;
        } else if (cand.querySelector("g.representation-group[id]")) {
          // Pure container group (e.g. PipingNetworkSystem) with no direct shapes of its own:
          // let its child segments/items handle the click unless a direct text label was clicked.
          const hasLabelHit = Array.from(cand.children).some((ch) => {
            if (ch.tagName.toLowerCase() !== "text") return false;
            const r = ch.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && cx >= r.left - pad && cx <= r.right + pad && cy >= r.top - pad && cy <= r.bottom + pad;
          });
          if (!hasLabelHit) continue;
        }
      }
    }
    const area = Math.max(rect.width, 1) * Math.max(rect.height, 1);
    const depth = domDepth(cand);
    if (area < bestArea || (area === bestArea && depth > bestDepth)) {
      best = cand;
      bestArea = area;
      bestDepth = depth;
    }
  }
  return best;
}

function pick(target: EventTarget | null, clientX?: number, clientY?: number): void {
  if (!pickable.value) return;
  const top = props.layers[props.layers.length - 1];
  const topHost = top ? hosts.get(top.id) ?? null : null;
  const layerEl = (target instanceof Element ? target.closest<HTMLElement>("[data-layer]") : null) ?? topHost;
  let group = target instanceof Element ? (target.closest("g.representation-group[id]") ?? target.closest("g[id]")) : null;
  if (isIgnoredGroup(group)) group = null;
  if (layerEl && clientX !== undefined && clientY !== undefined) {
    const PAD = 6;
    if (!group) {
      group = findSmallestMatchingGroup(layerEl, clientX, clientY, PAD);
    } else {
      // Even when clicking inside a parent container (equipment/line/segment), prefer a smaller
      // nested representation-group (valve, fitting, nozzle) whose padded bbox covers the click.
      const inner = findSmallestMatchingGroup(group, clientX, clientY, PAD);
      if (inner) group = inner;
    }
  }
  if (!group || !layerEl) return emitPick(null);
  cursor.value = null;
  emitPick(pickedOf(group, layerEl.dataset.layer ?? ""));
}

/**
 * The objects a click can pick, in drawing order, in the top layer (the one taking pointer
 * events): as `pick` finds them, every DEXPI representation group, and any other group with an
 * id that is not inside one.
 */
function objects(): { layer: string; groups: Element[] } {
  const top = props.layers[props.layers.length - 1];
  const host = top ? hosts.get(top.id) : undefined;
  if (!top || !host) return { layer: "", groups: [] };
  const groups = Array.from(host.querySelectorAll("g[id]")).filter(
    (g) => !g.id.startsWith("diagram-") && (g.classList.contains("representation-group") || !g.parentElement?.closest("g.representation-group[id]")),
  );
  return { layer: top.id, groups };
}

/** N/P: mark and centre the next or previous object, and say which it is. */
function stepObject(direction: 1 | -1): void {
  const { layer, groups } = objects();
  if (!groups.length) {
    announcement.value = "No objects to pick in this drawing.";
    return;
  }
  const at = groups.findIndex((g) => g.id === cursor.value);
  const index = at < 0 ? (direction > 0 ? 0 : groups.length - 1) : (at + direction + groups.length) % groups.length;
  const group = groups[index]!;
  cursor.value = group.id;
  // Where it is drawn in the layer the mark is on (an overlay's new sheet, not the old one below).
  focus(group.id, layer);
  const texts = pickedOf(group, "").texts.filter((t) => t !== group.id);
  announcement.value = `${group.id}${texts.length ? ` (${texts.slice(0, 3).join(", ")})` : ""}, ${index + 1} of ${groups.length}. Enter shows its details.`;
}

/** Enter or Space: pick the object N/P stepped to. */
function pickCursor(): void {
  const { layer, groups } = objects();
  const group = groups.find((g) => g.id === cursor.value);
  if (!group) {
    announcement.value = "Press N or P to step to an object first.";
    return;
  }
  emitPick(pickedOf(group, layer));
  announcement.value = `${group.id} selected.`;
}

const help = computed(() =>
  [
    "Arrow keys pan, plus and minus zoom, 0 fits the drawing.",
    pickable.value ? "N steps to the next object and P (or Shift+N) to the previous one, Enter shows the details of the marked one, Escape clears the selection." : "",
  ]
    .filter(Boolean)
    .join(" "),
);
const helpId = `${useId()}-help`;
</script>

<template>
  <div
    ref="container"
    class="pane"
    :class="{ dragging, pickable }"
    tabindex="0"
    role="application"
    :aria-label="label ? `${label}: drag to pan, wheel to zoom` : 'Drawing: drag to pan, wheel to zoom'"
    :aria-describedby="helpId"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="onPointerUp"
    @pointercancel="onPointerUp"
    @wheel.prevent="onWheel"
    @keydown="onKey"
  >
    <div v-if="label" class="caption">{{ label }}</div>
    <p :id="helpId" class="sr-only">{{ help }}</p>
    <p class="sr-only" aria-live="polite" data-testid="pane-announcement">{{ announcement }}</p>
    <div
      class="stage"
      :style="{ transform: transformOf(view), width: `${size.width}px`, height: `${size.height}px` }"
    >
      <div
        v-for="(layer, index) in layers"
        :id="layer.id"
        :key="layer.id"
        :ref="(el) => setHost(layer.id, el)"
        class="layer"
        :data-layer="layer.id"
        :style="{ opacity: layer.opacity ?? 1, pointerEvents: index === layers.length - 1 ? 'auto' : 'none' }"
      />
    </div>
    <p v-if="!hasDrawing" class="empty">{{ emptyText }}</p>
    <div class="controls" @pointerdown.stop>
      <button class="btn" type="button" title="Zoom in (+)" aria-label="Zoom in" @click="zoomBy(1.25)">+</button>
      <button class="btn" type="button" title="Zoom out (-)" aria-label="Zoom out" @click="zoomBy(0.8)">&minus;</button>
      <button class="btn" type="button" title="Fit to view (0)" @click="fit">Fit</button>
      <button class="btn" type="button" title="Print scale (1 mm = 1 mm)" @click="actualSize">1:1</button>
    </div>
  </div>
</template>

<style scoped>
.pane {
  position: relative;
  overflow: hidden;
  background: var(--bg-sunk);
  touch-action: none;
  cursor: grab;
  user-select: none;
  min-height: 240px;
  outline: none;
}
.pane:focus-visible {
  box-shadow: inset 0 0 0 2px var(--accent);
}
.pane.dragging {
  cursor: grabbing;
}
.stage {
  position: absolute;
  left: 0;
  top: 0;
  transform-origin: 0 0;
  background: var(--paper);
  box-shadow: var(--shadow);
}
.layer {
  position: absolute;
  inset: 0;
}
.layer :deep(svg) {
  display: block;
  width: 100%;
  height: 100%;
}
.pickable .layer :deep(g[id]) {
  cursor: pointer;
}
.pickable .layer :deep(svg g.representation-group) {
  pointer-events: visiblePainted;
  cursor: pointer;
}
.caption {
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 2;
  padding: 2px 8px;
  border-radius: var(--radius);
  background: var(--surface);
  border: 1px solid var(--border);
  font-size: var(--text-sm);
  font-weight: 600;
  pointer-events: none;
  max-width: calc(100% - 16px);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  margin: 0;
  color: var(--fg-muted);
  pointer-events: none;
}
.controls {
  position: absolute;
  right: 8px;
  bottom: 8px;
  z-index: 2;
  display: flex;
  gap: 4px;
  cursor: default;
}
.controls .btn {
  min-width: 34px;
  justify-content: center;
  box-shadow: var(--shadow);
}
</style>
