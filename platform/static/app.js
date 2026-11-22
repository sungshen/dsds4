'use strict';

const $ = (selector) => document.querySelector(selector);
const state = { items: [], selected: null, nextCursor: null, query: {}, view: 'explore', gold: [], goldTotal: 0, goldQuery: {}, goldEpoch: 0, goldNextOffset: 0, goldSnapshot: '', searchEpoch: 0, detailEpoch: 0 };
const labels = { explore: '데이터 탐색', gold: 'Gold 데이터', connections: '연결 상태' };
const formatNumber = (value) => value == null ? '—' : Number(value).toLocaleString('ko-KR', { maximumFractionDigits: 2 });
const safe = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const display = (value) => value == null ? '미확인' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const byteSize = (value) => value == null ? '' : value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${(value / 1024).toFixed(1)} KB`;

async function request(path, options = {}) {
  const response = await fetch(path, { headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `요청을 처리하지 못했습니다 (${response.status})`);
  return data;
}

function queryString(values) {
  return new URLSearchParams(Object.entries(values).filter(([, value]) => value !== '' && value != null)).toString();
}

function empty(target, title, description = '', error = false) {
  target.innerHTML = `<div class="empty${error ? ' error' : ''}"><span class="empty-icon" aria-hidden="true">${error ? '!' : '◫'}</span><strong>${safe(title)}</strong>${description ? `<p>${safe(description)}</p>` : ''}</div>`;
}

function loading(target, text = '데이터를 불러오는 중입니다') {
  target.innerHTML = `<div class="loading-wrapper"><div class="loading-bar"></div><div class="empty"><strong>${safe(text)}</strong></div></div>`;
}

function notify(text, isError = false) {
  const notice = $('#notice');
  notice.textContent = text;
  notice.classList.toggle('error', isError);
  notice.hidden = false;
}

function clearNotice() { $('#notice').hidden = true; }

function statusClass(status) {
  return ['connected', 'ready', 'ok', 'available'].includes(String(status).toLowerCase()) ? 'connected' : ['error', 'failed', 'unreachable'].includes(String(status).toLowerCase()) ? 'failed' : '';
}

function statusLabel(status) {
  return { connected: '연결됨', ready: '준비됨', ok: '연결됨', available: '사용 가능', configured: '설정됨', missing: '설정 필요', not_configured: '설정 필요', unconfigured: '설정 필요', disconnected: '연결 대기', error: '연결 오류', failed: '연결 오류', unchecked: '확인 전' }[status] || status;
}

async function loadStatus(check = false) {
  try {
    const data = await request(check ? '/api/connections/check' : '/api/status', check ? { method: 'POST' } : {});
    const connections = data.connections || [];
    const ready = connections.filter((connection) => statusClass(connection.status) === 'connected').length;
    $('#connection-summary').textContent = `${ready}/${connections.length} 연결됨`;
    $('#connection-summary').classList.toggle('ready', ready > 0);
    $('#connection-cards').innerHTML = connections.map((connection) => `<article class="connection-card"><div class="connection-card-top"><h2>${safe(connection.name)}</h2><span class="status-pill ${statusClass(connection.status)}">${safe(statusLabel(connection.status))}</span></div><p>${safe(connection.detail)}</p></article>`).join('');
    if (!connections.length) empty($('#connection-cards'), '연결 정보가 없습니다');
  } catch (error) {
    $('#connection-summary').textContent = '연결 확인 실패';
    empty($('#connection-cards'), '상태를 확인할 수 없습니다', error.message, true);
  }
}

function searchValues() {
  return Object.fromEntries(new FormData($('#search-form')));
}

async function search(append = false) {
  clearNotice();
  const epoch = ++state.searchEpoch;
  state.detailEpoch += 1;
  const values = append ? state.query : searchValues();
  if (values.start && values.end && values.start > values.end) return notify('종료일은 시작일 이후로 지정해 주세요.', true);
  if (!append) {
    state.items = [];
    state.selected = null;
    state.query = values;
    $('#pagination').hidden = true;
    $('#result-count').textContent = '0';
    $('#detail-label').textContent = '';
    empty($('#detail'), '관측을 선택하세요', '영상, 위치와 품질 정보가 표시됩니다.');
    loading($('#results'));
  }
  $('#search-button').disabled = true;
  $('#load-more').disabled = true;
  try {
    const data = await request(`/api/observations?${queryString({ ...values, limit: 30, cursor: append ? state.nextCursor : null })}`);
    if (epoch !== state.searchEpoch) return;
    const known = new Set(state.items.map((item) => item.id));
    state.items.push(...(data.items || []).filter((item) => !known.has(item.id)));
    state.nextCursor = data.next_cursor;
    renderResults();
  } catch (error) {
    if (epoch !== state.searchEpoch) return;
    if (append) notify(error.message, true);
    else empty($('#results'), '관측을 불러올 수 없습니다', error.message, true);
  } finally {
    if (epoch === state.searchEpoch) {
      $('#search-button').disabled = false;
      $('#load-more').disabled = false;
    }
  }
}

function renderResults() {
  $('#result-count').textContent = formatNumber(state.items.length);
  $('#pagination').hidden = !state.nextCursor;
  if (!state.items.length) return empty($('#results'), '검색 결과가 없습니다', '기간, 행정동 코드 또는 데이터 연결을 확인해 주세요.');
  $('#results').innerHTML = state.items.map((item, index) => `<button class="observation${state.selected?.id === item.id ? ' selected' : ''}" data-index="${index}" aria-pressed="${state.selected?.id === item.id}"><span class="observation-title">${safe(item.name || item.id)}</span><span class="observation-meta"><span class="stage-tag ${item.stage === 'gold' ? 'gold' : ''}">${safe(item.stage || 'Silver')}</span><span>${safe(item.date || item.observation_time || '관측시각 미확인')}</span><span>${item.source === 'local' ? '천리안 · DN' : item.stage === 'gold' ? 'VIIRS · Gold' : 'VIIRS · I5'}</span>${item.size != null ? `<span>${byteSize(item.size)}</span>` : ''}</span></button>`).join('');
}

function pointPlot(points) {
  const valid = points.filter((point) => Number.isFinite(point.longitude) && Number.isFinite(point.latitude));
  if (!valid.length) return '';
  const xs = valid.map((point) => point.longitude), ys = valid.map((point) => point.latitude);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const middleLat = (minY + maxY) / 2;
  const xRange = Math.max((maxX - minX) * Math.cos(middleLat * Math.PI / 180), 0.001);
  const yRange = Math.max(maxY - minY, 0.001);
  const scale = Math.min(480 / xRange, 270 / yRange);
  const x = (value) => 280 + ((value - (minX + maxX) / 2) * Math.cos(middleLat * Math.PI / 180)) * scale;
  const y = (value) => 165 - (value - (minY + maxY) / 2) * scale;
  const values = valid.map((point) => point.value).filter(Number.isFinite);
  const min = values.length ? Math.min(...values) : null, max = values.length ? Math.max(...values) : null;
  const circles = valid.map((point) => {
    const ratio = Number.isFinite(point.value) ? (point.value - min) / (max - min || 1) : null;
    const color = ratio == null ? '#c8d2df' : `hsl(${215 - ratio * 175} 70% ${44 + ratio * 9}%)`;
    return `<circle cx="${x(point.longitude).toFixed(2)}" cy="${y(point.latitude).toFixed(2)}" r="2.4" fill="${color}"><title>${point.longitude.toFixed(4)}, ${point.latitude.toFixed(4)} · ${point.value == null ? '품질 제외 또는 결측' : formatNumber(point.value) + ' K'}</title></circle>`;
  }).join('');
  return `<div class="preview"><svg viewBox="0 0 560 330" role="img" aria-label="경위도에 표시한 VIIRS 관측 픽셀"><path d="M35 295H525M35 35V295" stroke="#dce5f1" fill="none"/><text x="40" y="317" fill="#8c9db4" font-size="11">${minX.toFixed(3)}°E</text><text x="525" y="317" text-anchor="end" fill="#8c9db4" font-size="11">${maxX.toFixed(3)}°E</text>${circles}</svg><div class="preview-legend">${values.length ? `${formatNumber(min)}–${formatNumber(max)} K` : '유효 온도 없음'}</div></div><p class="preview-caption">원천 위경도 좌표 · ${formatNumber(valid.length)}개 표시 픽셀 · 회색은 품질 제외 또는 결측</p>`;
}

function observationPreview(data, item) {
  const points = data.points || [];
  const hasMap = points.some((point) => Number.isFinite(point.longitude) && Number.isFinite(point.latitude));
  let imageURL;
  if (data.preview_url) {
    const candidate = new URL(data.preview_url, window.location.origin);
    if (candidate.origin === window.location.origin) imageURL = candidate;
  }
  if (!imageURL) return pointPlot(points);

  const preview = data.preview || {};
  const nativeScan = preview.kind === 'native_scan';
  let caption = '원천 픽셀 미리보기 · 표시 대비 조정';
  let legend = '';
  if (nativeScan) {
    caption = `원본 스캔 행·열 ${formatNumber(preview.width)} × ${formatNumber(preview.height)}픽셀 · 위쪽이 북쪽인 지도는 아닙니다. 보간 없이 표시. 낮은 밝기온도는 밝게 표시합니다. 회색은 품질 제외 또는 결측, 투명 영역은 선택 범위 밖입니다.`;
    const min = preview.temperature_min_k;
    const max = preview.temperature_max_k;
    legend = `<div class="preview-legend">${min == null || max == null ? '유효 온도 없음' : `${formatNumber(min)}–${formatNumber(max)} K`}</div>`;
  }
  const image = `<div class="preview${nativeScan ? ' native-preview' : ''}"><img src="${safe(imageURL.pathname + imageURL.search)}" alt="${safe(item.name)}의 픽셀 미리보기">${legend}</div><p class="preview-caption">${safe(caption)}</p>`;
  if (!hasMap) return image;

  return `<div class="preview-switch" role="group" aria-label="미리보기 방식"><button type="button" data-preview-view="image" aria-pressed="true">장면 영상</button><button type="button" data-preview-view="map" aria-pressed="false">위치 표본</button></div><div id="preview-image">${image}</div><div id="preview-map" hidden>${pointPlot(points)}</div>`;
}

function bindPreviewControls() {
  for (const button of document.querySelectorAll('[data-preview-view]')) {
    button.addEventListener('click', () => {
      const showImage = button.dataset.previewView === 'image';
      $('#preview-image').hidden = !showImage;
      $('#preview-map').hidden = showImage;
      for (const choice of document.querySelectorAll('[data-preview-view]')) {
        choice.setAttribute('aria-pressed', String(choice === button));
      }
    });
  }
}

function statEntries(stats) {
  const preferred = [
    ['valid_pixel_count', '유효 픽셀'], ['valid_count', '유효 픽셀'], ['pixel_count', '전체 픽셀'],
    ['temperature_mean_k', '평균 밝기온도 K'], ['mean_k', '평균 밝기온도 K'], ['mean', '평균값'], ['dn_mean', '평균 DN'],
    ['min', '최솟값'], ['max', '최댓값'], ['coverage_fraction', '관측 범위 비율'], ['valid_fraction', '유효 비율'],
  ];
  const seenLabels = new Set();
  const entries = preferred.filter(([key, label]) => {
    if (!(key in stats) || seenLabels.has(label)) return false;
    seenLabels.add(label);
    return true;
  }).map(([key, label]) => [label, stats[key]]);
  if (!entries.length) return Object.entries(stats).slice(0, 3);
  return entries.slice(0, 3);
}

async function selectObservation(index, swath = '') {
  const item = state.items[index];
  if (!item) return;
  state.selected = item;
  const epoch = ++state.detailEpoch;
  renderResults();
  $('#detail-label').textContent = item.source === 'local' ? '천리안 IR112' : `VIIRS · ${item.stage === 'gold' ? 'Gold' : 'Silver'}`;
  loading($('#detail'), '관측 데이터를 읽고 있습니다');
  try {
    const data = await request(`/api/observations/${encodeURIComponent(item.id)}/detail?${queryString({ dong: state.query.dong, swath })}`);
    if (epoch !== state.detailEpoch) return;
    const columns = data.columns || [];
    const units = data.metadata?.units;
    const i5 = columns.includes('i05_brightness_temperature_k');
    let detailLabel = `VIIRS · ${item.stage === 'gold' ? 'Gold' : 'Silver'}`;
    if (item.source === 'local') detailLabel = '천리안 IR112 · DN';
    else if (i5) detailLabel = `VIIRS I5 · ${units || 'K'}`;
    else if (units && units !== 'product defined') detailLabel += ` · ${units}`;
    $('#detail-label').textContent = detailLabel;
    const entries = statEntries(data.stats || {});
    const preview = observationPreview(data, item);
    const warnings = (data.warnings || []).filter((warning) => typeof warning === 'string');
    const warningMarkup = warnings.length ? `<div class="observation-warning" role="status">${warnings.map((warning) => `<p>${safe(warning)}</p>`).join('')}</div>` : '';
    const metadata = { ...(data.metadata || {}) };
    const scenes = data.scenes || [];
    const scenePicker = scenes.length ? `<label class="scene-picker">관측 장면<select id="scene-filter"><option value="">전체 ${formatNumber(scenes.length)}개 장면 · 통계 합산</option>${scenes.map((scene) => `<option value="${safe(scene.swath_id)}"${scene.swath_id === data.selected_swath ? ' selected' : ''}>${safe(scene.swath_id)} · ${safe(scene.swath_start_utc || '시각 미확인')} · ${formatNumber(scene.pixel_count)}픽셀</option>`).join('')}</select></label>` : '';

    $('#detail').innerHTML = `<div class="detail-body"><h3 class="detail-title">${safe(item.name)}</h3><p class="detail-meta">${safe(item.observation_time || item.date)}${item.boundary_version ? ` · ${safe(item.boundary_version)}` : ''}</p>${warningMarkup}${scenePicker}${preview}${entries.length ? `<div class="stat-grid">${entries.map(([key, value]) => `<div class="stat"><span class="stat-label">${safe(key)}</span><span class="stat-value">${safe(typeof value === 'number' ? formatNumber(value) : display(value))}</span></div>`).join('')}</div>` : ''}<div class="detail-section"><h3 class="section-label">원천 · 처리 정보</h3><dl class="metadata">${Object.entries(metadata).map(([key, value]) => `<div class="metadata-row"><dt>${safe(key)}</dt><dd>${safe(display(value))}</dd></div>`).join('')}</dl></div>${data.rows?.length ? `<div class="detail-section"><h3 class="section-label">레코드 미리보기</h3>${table(data.columns || Object.keys(data.rows[0]), data.rows)}</div>` : ''}</div>`;
    bindPreviewControls();
    const imageElement = $('#detail img');
    imageElement?.addEventListener('error', () => empty(imageElement.parentElement, '미리보기를 불러오지 못했습니다'));
    $('#scene-filter')?.addEventListener('change', (event) => selectObservation(index, event.target.value));
  } catch (error) {
    if (epoch === state.detailEpoch) empty($('#detail'), '관측을 열 수 없습니다', error.message, true);
  }
}

function table(columns, rows) {
  return `<div class="table-scroll"><table><thead><tr>${columns.map((column) => `<th scope="col">${safe(column)}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((column, index) => `<td title="${safe(display(Array.isArray(row) ? row[index] : row[column]))}">${safe(display(Array.isArray(row) ? row[index] : row[column]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

async function loadGold(append = false) {
  clearNotice();
  const epoch = ++state.goldEpoch;
  const values = append ? state.goldQuery : Object.fromEntries(new FormData($('#gold-form')));
  if (values.start && values.end && values.start > values.end) return notify('종료일은 시작일 이후로 지정해 주세요.', true);
  if (!append) {
    state.gold = [];
    state.goldNextOffset = 0;
    state.goldSnapshot = '';
    state.goldQuery = values;
    $('#gold-pagination').hidden = true;
    $('#gold-count').textContent = '0';
    loading($('#gold-results'), 'R2에 게시된 Gold를 읽고 있습니다');
  }
  $('#gold-more').disabled = true;
  $('#gold-form button').disabled = true;
  try {
    const data = await request(`/api/gold?${queryString({ ...values, limit: 100, offset: state.goldNextOffset, snapshot: state.goldSnapshot })}`);
    if (epoch !== state.goldEpoch) return;
    state.gold.push(...(data.items || []));
    state.goldNextOffset = data.next_offset;
    state.goldSnapshot = data.snapshot || '';
    state.goldTotal = data.total;
    $('#gold-count').textContent = data.total == null ? `${formatNumber(state.gold.length)}+` : formatNumber(data.total);
    $('#gold-pagination').hidden = !data.has_more;
    if (!state.gold.length) return empty($('#gold-results'), '게시된 Gold 데이터가 없습니다', '현재 기간의 R2 Gold 파일과 재처리 진행 상황을 확인해 주세요.');
    const columns = ['date', 'dong_code', 'boundary_version', 'swath_id', 'swath_start_utc', 'pixel_count', 'valid_count', 'temperature_mean_k', 'temperature_min_k', 'temperature_max_k', 'valid_fraction', 'generation', 'publication_status'];
    $('#gold-results').innerHTML = table(columns, state.gold) + `<div class="mini-caption">R2 원본 Gold · ${formatNumber(state.gold.length)}행 표시${data.has_more ? ' · 다음 행 추가 조회 가능' : ' · 조회 완료'} · 여러 장면의 반복 관측은 각각 집계됩니다. verified가 아닌 파일은 전체 관측 재처리 완료 여부가 미검증입니다.</div>`;
  } catch (error) {
    if (epoch !== state.goldEpoch) return;
    if (append) notify(error.message, true);
    else empty($('#gold-results'), 'Gold를 조회할 수 없습니다', error.message, true);
  } finally {
    if (epoch === state.goldEpoch) {
      $('#gold-more').disabled = false;
      $('#gold-form button').disabled = false;
    }
  }
}

function changeView(view) {
  if (!labels[view]) return;
  state.view = view;
  clearNotice();
  for (const section of document.querySelectorAll('.view')) section.hidden = section.id !== `view-${view}`;
  for (const button of document.querySelectorAll('.nav-item')) {
    button.classList.toggle('active', button.dataset.view === view);
    if (button.dataset.view === view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  $('#breadcrumb').textContent = labels[view];
  if (view === 'gold') loadGold();
  if (view === 'connections') loadStatus();
}

$('#search-form').addEventListener('submit', (event) => { event.preventDefault(); search(); });
$('#gold-form').addEventListener('submit', (event) => { event.preventDefault(); loadGold(); });
$('#load-more').addEventListener('click', () => search(true));
$('#gold-more').addEventListener('click', () => loadGold(true));
$('#results').addEventListener('click', (event) => { const button = event.target.closest('.observation'); if (button) selectObservation(Number(button.dataset.index)); });
for (const button of document.querySelectorAll('.nav-item')) button.addEventListener('click', () => changeView(button.dataset.view));
$('#refresh-status').addEventListener('click', () => loadStatus(true));
$('#connections-refresh').textContent = '연결 확인';
$('#connections-refresh').addEventListener('click', () => loadStatus(true));
function updateDongHint() {
  const local = $('#source').value === 'local';
  $('#dong').placeholder = local ? '10자리 행정동 코드 · 전체는 비워두세요' : '8자리 SGIS 코드 · 전체는 비워두세요';
  $('#dong').title = local ? '서울 행정동 adm_cd2 코드' : 'VIIRS 경계의 SGIS ADM_CD 코드';
}
$('#source').addEventListener('change', updateDongHint);
$('#gold-form input[name="dong"]').placeholder = '8자리 SGIS 코드 · 전체는 비워두세요';
updateDongHint();
loadStatus();
search();
