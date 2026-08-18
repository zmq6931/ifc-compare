// ifc-compare 3D 对比查看器
// 双场景 + 共享相机 + scissor 分割渲染，实现同步旋转的左右拖拽对比。
window.__appStarted = true;

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const STATUS = {
  added:     { label: 'Added', color: '#16a34a', text: '#15803d', bg: '#e8f7ee' },
  deleted:   { label: 'Deleted', color: '#ff0000', text: '#b91c1c', bg: '#fdecec' },
  geom:      { label: 'Geometry', color: '#eab308', text: '#a16207', bg: '#fef3c7' },
  param:     { label: 'Parameters', color: '#2563eb', text: '#1d4ed8', bg: '#e7eefc' },
  both:      { label: 'Both', color: '#7c3aed', text: '#6d28d9', bg: '#f0eafd' },
  unchanged: { label: 'Unchanged', color: '#94a3b8', text: '#475569', bg: '#eef1f4' },
};

const $ = (sel) => document.querySelector(sel);

const viewport = $('#viewport');
const canvas = $('#c3d');
const statusEl = $('#status');

const state = {
  mode: 'split',
  split: 0.5,
  visible: { added: true, deleted: true, geom: true, param: true, both: true, unchanged: true },
  meshGroups: {},   // status -> [{mesh, side}]
  report: null,
};

let renderer, camera, controls;
let sceneOld, sceneNew, groupOld, groupNew;
let dirOld, dirNew;
let gridOld = null, gridNew = null;
const bgColor = new THREE.Color(0xf2f4f7);
let lightOffset = new THREE.Vector3(0.55, -0.45, 1.0).multiplyScalar(1000);

function setStatus(msg, isError = false) {
  statusEl.textContent = msg;
  statusEl.classList.toggle('err', !!isError);
}

function basename(p) {
  return String(p || '').split(/[\\/]/).pop() || '';
}

function esc(s) {
  return String(s == null ? '' : s)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function fmtVal(v) {
  if (v === null || v === undefined) return '—';
  if (Array.isArray(v)) return v.map(fmtVal).join(', ');
  return String(v);
}

// ---------------------------------------------------------------- renderer

function initRenderer() {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

  camera = new THREE.PerspectiveCamera(45, 1, 0.5, 500000);
  camera.up.set(0, 0, 1);
  camera.position.set(4000, -6000, 4500);

  sceneOld = new THREE.Scene();
  sceneNew = new THREE.Scene();
  sceneOld.background = bgColor;
  sceneNew.background = bgColor;

  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;

  dirOld = addLights(sceneOld);
  dirNew = addLights(sceneNew);

  window.addEventListener('resize', resize);
  resize();
  requestAnimationFrame(loop);
}

function addLights(scene) {
  scene.add(new THREE.HemisphereLight(0xffffff, 0x99a2b0, 1.0));
  const dir = new THREE.DirectionalLight(0xffffff, 1.6);
  scene.add(dir);
  return dir;
}

function resize() {
  const w = viewport.clientWidth, h = viewport.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

function loop() {
  requestAnimationFrame(loop);
  controls.update();
  dirOld.position.copy(camera.position).add(lightOffset);
  dirNew.position.copy(camera.position).add(lightOffset);
  render();
}

function render() {
  const w = viewport.clientWidth, h = viewport.clientHeight;
  if (!w || !h) return;
  if (state.mode === 'split') {
    sceneOld.background = bgColor;
    sceneNew.background = bgColor;
    if (gridOld) gridOld.visible = true;
    if (gridNew) gridNew.visible = true;
    const x = Math.round(w * state.split);
    // 照片对比滑块原理：两个场景都按【完整视口】渲染（投影比例恒定，不随分割线变化），
    // scissor 只裁剪每个场景可见的像素区域。拖拽分割线 = 只改变左右显示多少。
    renderer.setScissorTest(true);
    renderer.setViewport(0, 0, w, h);
    renderer.setScissor(0, 0, x, h);
    renderer.render(sceneOld, camera);
    renderer.setViewport(0, 0, w, h);
    renderer.setScissor(x, 0, w - x, h);
    renderer.render(sceneNew, camera);
    renderer.setScissorTest(false);
  } else {
    // 合并视图：第一遍渲染新场景（带背景+清屏），第二遍渲染旧场景。
    // 旧场景必须去掉背景色，否则它的全屏背景 quad 会覆盖第一遍已画好的新模型画面；
    // 旧场景的网格线也与新场景完全同位，合并时隐藏避免线体闪烁。
    sceneNew.background = bgColor;
    sceneOld.background = null;
    if (gridOld) gridOld.visible = false;
    if (gridNew) gridNew.visible = true;
    renderer.setViewport(0, 0, w, h);
    renderer.setScissorTest(false);
    renderer.render(sceneNew, camera);
    renderer.autoClear = false;
    renderer.render(sceneOld, camera);
    renderer.autoClear = true;
  }
}

// ---------------------------------------------------------------- loading

const loader = new GLTFLoader();

async function loadModels() {
  // 时间戳参数绕过浏览器旧缓存；.bin 文件名为内容哈希（见 export.py），天然不会缓存错版本
  const t = Date.now();
  const [r1, r2] = await Promise.all([
    loader.loadAsync(`models/old.gltf?t=${t}`),
    loader.loadAsync(`models/new.gltf?t=${t}`),
  ]);
  groupOld = r1.scene;
  groupNew = r2.scene;
  sceneOld.add(groupOld);
  sceneNew.add(groupNew);

  collectMeshes(groupOld, 'old');
  collectMeshes(groupNew, 'new');
  postProcessMaterials(groupOld);
  postProcessMaterials(groupNew);
  addGrids();
  applyVisibility();
  fit();
}

function collectMeshes(root, side) {
  root.traverse((obj) => {
    if (obj.isMesh && STATUS[obj.name]) {
      (state.meshGroups[obj.name] = state.meshGroups[obj.name] || []).push({ mesh: obj, side });
    }
  });
}

function postProcessMaterials(root) {
  root.traverse((obj) => {
    if (!obj.isMesh) return;
    const m = obj.material;
    if (!m) return;
    // glTF 未携带法线：flatShading 让三渲二直接按面着色，避免自动平滑法线把建筑体量“磨圆”
    m.flatShading = true;
    // 高亮构件自带状态色微光，暗面/细小构件更醒目（未变灰除外）
    const st = STATUS[obj.name];
    if (st && obj.name !== 'unchanged' && st.color) {
      m.emissive = new THREE.Color(st.color);
      m.emissiveIntensity = 0.3;
    }
    obj.userData.baseTransparent = !!m.transparent;
    obj.userData.baseOpacity = m.opacity ?? 1;
    if (m.transparent) {
      m.depthWrite = false; // 半透明“未变”构件不遮挡高亮构件
      obj.renderOrder = 1;
    } else {
      obj.renderOrder = 0;
    }
  });
}

function addGrids() {
  const box = new THREE.Box3();
  if (groupOld) box.setFromObject(groupOld);
  if (groupNew) box.union(new THREE.Box3().setFromObject(groupNew));
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3());
  const span = Math.max(size.x, size.y) * 4 || 100;
  gridOld = new THREE.GridHelper(span, 40, 0x8a93a3, 0xd8dde3);
  gridNew = new THREE.GridHelper(span, 40, 0x8a93a3, 0xd8dde3);
  for (const grid of [gridOld, gridNew]) {
    // GridHelper 默认生成在 XZ 平面（three 的 Y-up 惯例）；IFC 是 Z-up，
    // 绕 X 轴转 90° 平铺到 XY 平面（地面），再放到模型底部高度。
    grid.rotation.x = Math.PI / 2;
    grid.position.z = box.min.z - 0.01;
  }
  sceneOld.add(gridOld);
  sceneNew.add(gridNew);
}

function fitToBox(box) {
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  const dist = (maxDim / 2 / Math.tan((camera.fov * Math.PI / 180) / 2)) * 1.5;
  const dir = new THREE.Vector3(0.55, -0.7, 0.45).normalize();
  camera.position.copy(center).addScaledVector(dir, dist);
  camera.near = Math.max(0.1, dist / 500);
  camera.far = dist * 30;
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.minDistance = maxDim / 100;
  controls.maxDistance = dist * 6;
  controls.update();
}

function fit() {
  const box = new THREE.Box3();
  if (groupOld) box.setFromObject(groupOld);
  if (groupNew) box.union(new THREE.Box3().setFromObject(groupNew));
  fitToBox(box);
}

function fitDifferences() {
  // 聚焦到所有可见的差异构件（排除未变），细小的构件也能拉到眼前
  const box = new THREE.Box3();
  let any = false;
  for (const [status, objs] of Object.entries(state.meshGroups)) {
    if (status === 'unchanged' || !state.visible[status]) continue;
    for (const { mesh } of objs) {
      if (!mesh.visible) continue;
      mesh.updateWorldMatrix(true, false);
      box.expandByObject(mesh);
      any = true;
    }
  }
  if (!any) {
    setStatus('No visible changed elements. Check the status toggles on top first.');
    return;
  }
  fitToBox(box);
  setStatus('Focused on changes — Green=Added  Red=Deleted  Yellow=Geometry  Blue=Parameters  Purple=Both');
}

// ---------------------------------------------------------------- divider

const dividerEl = $('#divider');

function setSplit(f) {
  state.split = Math.min(0.95, Math.max(0.05, f));
  dividerEl.style.left = `${(state.split * 100).toFixed(2)}%`;
}

function setupDivider() {
  let dragging = false;
  dividerEl.addEventListener('pointerdown', (e) => {
    dragging = true;
    dividerEl.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  dividerEl.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const rect = viewport.getBoundingClientRect();
    setSplit((e.clientX - rect.left) / rect.width);
  });
  const end = () => { dragging = false; };
  dividerEl.addEventListener('pointerup', end);
  dividerEl.addEventListener('pointercancel', end);

  // 支持 ?split=0.3 / ?mode=merged 预设视图状态
  const presetSplit = new URLSearchParams(location.search).get('split');
  if (presetSplit) setSplit(parseFloat(presetSplit));
  const presetMode = new URLSearchParams(location.search).get('mode');
  if (presetMode === 'merged') {
    document.body.classList.add('merged');
    document.querySelectorAll('#mode-switch button').forEach((b) =>
      b.classList.toggle('active', b.dataset.mode === 'merged'));
    state.mode = 'merged';
  }
}

// ---------------------------------------------------------------- report UI

function renderCounts() {
  const counts = state.report.meta.counts;
  for (const key of Object.keys(counts)) {
    const el = $(`#cnt-${key}`);
    if (el) el.textContent = counts[key];
  }
  $('#brand-ver').textContent = state.report.meta.version || '';
  $('#label-old').textContent = `Old · ${basename(state.report.meta.oldFile)}`;
  $('#label-new').textContent = `New · ${basename(state.report.meta.newFile)}`;
}

function renderStats() {
  const counts = state.report.meta.counts;
  const items = [
    ['added', STATUS.added.label, counts.added],
    ['deleted', STATUS.deleted.label, counts.deleted],
    ['geom', STATUS.geom.label, counts.geom],
    ['param', STATUS.param.label, counts.param],
    ['both', STATUS.both.label, counts.both],
    ['unchanged', STATUS.unchanged.label, counts.unchanged],
  ];
  $('#stats').innerHTML = items.map(([key, label, n]) => `
    <div class="stat">
      <div class="stat-label"><i class="dot" style="--c:${STATUS[key].color}"></i>${label}</div>
      <b style="color:${STATUS[key].text}">${n}</b>
    </div>`).join('');
}

function propsTable(properties) {
  const rows = [];
  for (const [pset, props] of Object.entries(properties || {})) {
    for (const [name, value] of Object.entries(props || {})) {
      rows.push(`<tr><td class="path">${esc(pset)}.${esc(name)}</td><td>${esc(fmtVal(value))}</td></tr>`);
    }
  }
  if (!rows.length) return '<div class="muted">No properties</div>';
  return `<table class="changes"><thead><tr><th>Property</th><th>Value</th></tr></thead><tbody>${rows.join('')}</tbody></table>`;
}

function itemHtml(key, item) {
  const isMod = key === 'geom' || key === 'param' || key === 'both';
  // 初始全部折叠，点击条目头再展开，避免长列表一进来就撑满面板
  const open = '';
  let body;
  if (isMod) {
    const rows = (item.changes || []).map((c) => {
      const tag = c.quantity ? '<span class="qto-tag">Qty</span>' : '';
      return `
      <tr>
        <td class="path">${tag}${esc(c.pset)}.${esc(c.property)}</td>
        <td class="v-old">${esc(fmtVal(c.old))}</td>
        <td class="v-new">${esc(fmtVal(c.new))}</td>
      </tr>`;
    }).join('');
    const geom = item.geometryChanged ? '<div class="geom-badge">Geometry/position changed</div>' : '';
    const tbl = rows
      ? `<table class="changes"><thead><tr><th>Property</th><th>Old</th><th>New</th></tr></thead><tbody>${rows}</tbody></table>`
      : (item.geometryChanged ? '<div class="muted">No property changes</div>' : '');
    body = `${tbl}${geom}`;
  } else {
    body = propsTable(item.properties);
  }
  const text = `${item.name} ${item.type} ${item.guid}`.toLowerCase();
  return `
  <article class="item${open}" data-text="${esc(text)}">
    <button class="item-head">
      <span class="badge" style="color:${STATUS[key].text};background:${STATUS[key].bg}">${STATUS[key].label}</span>
      <span class="iname">${esc(item.name)}</span>
      <span class="itype">${esc(item.type)}</span>
      <span class="chev"></span>
    </button>
    <div class="item-body">
      <div class="guid">GUID&nbsp;${esc(item.guid)}</div>
      ${body}
    </div>
  </article>`;
}

function renderPanel() {
  renderStats();
  const { added, deleted, changed } = state.report.elements;
  const changedList = changed || [];
  const sections = [
    ['added', added],
    ['deleted', deleted],
    ['geom', changedList.filter((c) => c.kind === 'geom')],
    ['param', changedList.filter((c) => c.kind === 'param')],
    ['both', changedList.filter((c) => c.kind === 'both')],
  ];
  $('#list').innerHTML = sections.map(([key, items]) => {
    if (!items.length) return '';
    return `<section><h3>${STATUS[key].label} · ${items.length}</h3>${items.map((it) => itemHtml(key, it)).join('')}</section>`;
  }).join('');
  if (!added.length && !deleted.length && !changedList.length) {
    $('#list').innerHTML = '<div class="muted empty">No reportable differences between the two models.</div>';
  }
  $('#list').addEventListener('click', (e) => {
    const head = e.target.closest('.item-head');
    if (head) head.closest('.item').classList.toggle('open');
  });
}

function setupSearch() {
  const input = $('#search');
  const list = $('#list');
  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    list.querySelectorAll('.item').forEach((item) => {
      item.style.display = item.dataset.text.includes(q) ? '' : 'none';
    });
    list.querySelectorAll('section').forEach((sec) => {
      const any = [...sec.querySelectorAll('.item')].some((i) => i.style.display !== 'none');
      sec.style.display = any ? '' : 'none';
    });
  });
}

// ---------------------------------------------------------------- toggles

function meshWorldBox(mesh) {
  mesh.updateWorldMatrix(true, false);
  return new THREE.Box3().setFromObject(mesh);
}

function applyVisibility() {
  const merged = state.mode === 'merged';
  // 调试/诊断：?hideold=1 隐藏旧场景全部构件，用于排查遮挡来源
  const hideOld = new URLSearchParams(location.search).get('hideold') === '1';
  // 合并视图：收集新版实心构件的包围盒，用于检测旧版构件与其空间重叠
  const newSolidBoxes = [];
  if (merged) {
    for (const [st, objs] of Object.entries(state.meshGroups)) {
      if (st === 'unchanged' || !state.visible[st]) continue;
      for (const { mesh, side } of objs) {
        if (side === 'new') newSolidBoxes.push(meshWorldBox(mesh));
      }
    }
  }
  for (const [status, objs] of Object.entries(state.meshGroups)) {
    const base = !!state.visible[status];
    for (const { mesh, side } of objs) {
      mesh.visible = base && !(merged && hideOld && side === 'old');
      const m = mesh.material;
      if (!m) continue;
      const baseTransparent = mesh.userData.baseTransparent === true;
      const baseOpacity = mesh.userData.baseOpacity ?? 1;
      let transparent = baseTransparent;
      let opacity = baseOpacity;
      let depthWrite = !baseTransparent;
      let depthTest = true;
      let renderOrder = baseTransparent ? 1 : 0;
      let offset = false;
      if (merged && base) {
        if (status === 'unchanged') {
          // 未变构件：合并视图作为背景层——保持基础透明度、不写深度且不测深度，绝不遮挡高亮
          transparent = true;
          opacity = baseOpacity;
          depthWrite = false;
          depthTest = false;
          renderOrder = -1;
        } else if (side === 'old') {
          const oldBox = meshWorldBox(mesh);
          // 与新版构件重叠：半透明显示（不隐藏）——删除红色更不透明些，新旧都能看到
          const overlap = newSolidBoxes.some((b) => b.intersectsBox(oldBox));
          if (overlap) {
            transparent = true;
            opacity = status === 'deleted' ? 0.5 : 0.15;
            depthWrite = false;
            renderOrder = 1;
          } else {
            offset = true;
          }
        }
      }
      if (
        m.transparent !== transparent || m.opacity !== opacity || m.depthWrite !== depthWrite
        || m.depthTest !== depthTest || mesh.renderOrder !== renderOrder || m.polygonOffset !== offset
      ) {
        m.transparent = transparent;
        m.opacity = opacity;
        m.depthWrite = depthWrite;
        m.depthTest = depthTest;
        m.polygonOffset = offset;
        m.polygonOffsetFactor = 1;
        m.polygonOffsetUnits = 1;
        mesh.renderOrder = renderOrder;
        m.needsUpdate = true;
      }
    }
  }
}

function setupUi() {
  document.querySelectorAll('#mode-switch button').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.mode = btn.dataset.mode;
      document.querySelectorAll('#mode-switch button').forEach((b) =>
        b.classList.toggle('active', b === btn));
      document.body.classList.toggle('merged', state.mode === 'merged');
      applyVisibility();
    });
  });

  document.querySelectorAll('#toggles input').forEach((cb) => {
    cb.addEventListener('change', () => {
      state.visible[cb.dataset.status] = cb.checked;
      applyVisibility();
    });
  });

  $('#btn-fit').addEventListener('click', fit);
  $('#btn-diff').addEventListener('click', fitDifferences);

  $('#btn-panel').addEventListener('click', () => document.body.classList.toggle('panel-hidden'));
  $('#panel-close').addEventListener('click', () => document.body.classList.add('panel-hidden'));

  if (window.innerWidth < 860) document.body.classList.add('panel-hidden');
}

// ---------------------------------------------------------------- loader

function setupLoader() {
  const dialog = $('#loader-dialog');
  const fileOld = $('#file-old');
  const fileNew = $('#file-new');
  const nameOld = $('#name-old');
  const nameNew = $('#name-new');
  const startBtn = $('#loader-start');
  const msgEl = $('#loader-msg');

  $('#btn-load').addEventListener('click', () => {
    dialog.hidden = false;
    msgEl.textContent = '';
    msgEl.classList.remove('err');
  });
  $('#loader-cancel').addEventListener('click', () => { dialog.hidden = true; });
  dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.hidden = true; });

  function sync() {
    nameOld.textContent = fileOld.files[0] ? fileOld.files[0].name : '';
    nameNew.textContent = fileNew.files[0] ? fileNew.files[0].name : '';
    startBtn.disabled = !(fileOld.files[0] && fileNew.files[0]);
  }
  fileOld.addEventListener('change', sync);
  fileNew.addEventListener('change', sync);

  function setMsg(text, isErr = false) {
    msgEl.textContent = text;
    msgEl.classList.toggle('err', isErr);
  }

  async function upload(slot, file) {
    const res = await fetch(`/api/upload?slot=${encodeURIComponent(slot)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    let data = {};
    try { data = await res.json(); } catch { /* non-JSON response */ }
    if (!res.ok || !data.ok) throw new Error(data.error || `Upload failed HTTP ${res.status}`);
  }

  startBtn.addEventListener('click', async () => {
    const f1 = fileOld.files[0];
    const f2 = fileNew.files[0];
    if (!f1 || !f2) return;
    startBtn.disabled = true;
    try {
      setMsg(`Uploading old model (${f1.name})…`);
      await upload('old', f1);
      setMsg(`Uploading new model (${f2.name})…`);
      await upload('new', f2);
      setMsg('Comparing models and exporting geometry. Large models can take a few minutes…');
      const res = await fetch('/api/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ oldName: f1.name, newName: f2.name }),
      });
      let data = {};
      try { data = await res.json(); } catch { /* non-JSON response */ }
      if (!res.ok || !data.ok) throw new Error(data.error || `Compare failed HTTP ${res.status}`);
      const c = data.counts || {};
      setMsg(`Done: added ${c.added} / deleted ${c.deleted} / geometry ${c.geom} / parameters ${c.param} / both ${c.both} / unchanged ${c.unchanged}. Refreshing report…`);
      location.href = (data.url || '/report.html') + '?t=' + Date.now();
    } catch (err) {
      console.error(err);
      setMsg('Failed: ' + err.message, true);
      startBtn.disabled = false;
    }
  });
}

// ---------------------------------------------------------------- main

async function main() {
  setupDivider();
  setupUi();
  setupSearch();
  setupLoader();
  setStatus('Loading…');

  try {
    initRenderer();
  } catch (err) {
    // If 3D is unavailable the list still works; show a clear message instead of a dead page
    console.error(err);
    setStatus(`Unable to create a WebGL context: ${err.message}. Enable browser hardware acceleration and refresh.`, true);
    return;
  }

  try {
    const res = await fetch(`diff.json?t=${Date.now()}`);
    if (!res.ok) throw new Error(`diff.json HTTP ${res.status}`);
    state.report = await res.json();
    renderCounts();
    renderPanel();
    setStatus('Report loaded, loading geometry…');
    await loadModels();
    setStatus('Ready — drag the divider to compare both sides; rotation & zoom stay in sync');
  } catch (err) {
    console.error(err);
    setStatus(`Failed to load: ${err.message}`, true);
  }
}

window.addEventListener('error', (e) => {
  if (!statusEl.textContent.startsWith('Failed to load') && !statusEl.textContent.startsWith('Runtime error')) {
    setStatus(`Runtime error: ${e.message}`, true);
  }
});

window.addEventListener('unhandledrejection', (e) => {
  const msg = e.reason && e.reason.message ? e.reason.message : String(e.reason);
  if (!statusEl.textContent.startsWith('Failed to load') && !statusEl.textContent.startsWith('Runtime error')) {
    setStatus(`Runtime error: ${msg}`, true);
  }
});

main();
