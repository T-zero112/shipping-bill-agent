let config, job, chosenFile, current;
let filter = 'all', selectedIndex = null, activeTab = 'evidence';
const $ = id => document.getElementById(id);
const operations={CREATE:'新增',UPDATE:'更新',NO_CHANGE:'无变化',PENDING_REVIEW:'待复核'};
const reasonText={'Free-service statement conflicts with monetary fee candidate; confirm service scope and applicability':'收费句与免费服务句存在冲突，请确认适用范围。','Current accounting entity is neither confirmed creditor nor debtor':'现有证据未确认记账主体是债权方或债务方。','Charge applicability is disputed by source evidence':'原文对该费用是否适用存在争议。','PDF processed locally; designated OCR verification still required':'当前使用本地 PDF 解析，本次尚未使用指定 OCR 验证。','Image processed with local OCR; designated OCR verification still required':'当前图片使用本地 OCR，本次尚未使用指定 OCR 验证。','Letterhead and payment recipient differ; agency/collection relationship is unconfirmed':'账单抬头与收款方不一致，需确认代理或代收关系。','Amount is missing, invalid, or not supported by exact source quote':'金额缺失、无效，或没有对应原文证明。','Missing or invalid ISO 4217 currency':'币种缺失或不是有效的 ISO 币种代码。','No complete evidence-backed creditor/debtor relationship':'缺少完整证据，无法确定收付款双方。','Accounting role or charging direction requires review':'记账主体的角色或收费方向需要人工确认。','Synthetic review example: amount and currency are not stated in the source':'合成复核示例：原文未注明这项费用的金额和币种。'};
const states={SUCCESS:'处理完成',PENDING_REVIEW:'需要复核',EMPTY_RESULT:'未提取到费用',UNRESOLVED:'OCR 未解决',PARSE_FAILED:'解析失败',EXTRACTION_FAILED:'提取失败',PARTIAL_FAILURE:'部分失败',OUTPUT_INVALID:'输出校验失败',INPUT_INVALID:'输入无效',PROCESSING_FAILED:'处理失败'};

function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}
function message(text) { $('message').textContent = text; }
function choose(file) {
  if (!file || $('submit').disabled) return;
  chosenFile = file;
  $('file-name').textContent = file.name; $('file-name').title = file.name;
  message(file.size > 30 * 1024 * 1024 ? '文件超过 30 MB，请选择较小的文件。' : '');
}
$('file').addEventListener('change', event => choose(event.target.files[0]));
for (const type of ['dragenter', 'dragover']) {
  $('drop').addEventListener(type, event => { event.preventDefault(); $('drop').classList.add('over'); });
}
for (const type of ['dragleave', 'drop']) {
  $('drop').addEventListener(type, event => {
    event.preventDefault(); $('drop').classList.remove('over');
    if (type === 'drop') choose(event.dataTransfer.files[0]);
  });
}
$('mode').addEventListener('change', () => {
  const remote = $('mode').value === 'remote';
  $('consent-wrap').hidden = !remote;
  $('mode-note').textContent = remote
    ? '仅支持 PDF；临时公网链接在处理结束后关闭。'
    : '需要本机 Ollama · 文件仅在本机处理 · 单文件不超过 30 MB';
});
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'X-Demo-Token': config.token, ...options.headers } });
  if (!response.ok) {
    let error;
    try { error = (await response.json()).error; } catch {}
    throw Error(error || '请求失败，请检查本地服务。');
  }
  return response;
}
function busy(value) {
  for (const id of ['submit', 'demo', 'entity', 'mode', 'file', 'consent']) $(id).disabled = value;
  $('drop').setAttribute('aria-disabled', String(value));
  $('submit').textContent = value ? '正在处理…' : '开始提取';
}
function clearResults() {
  current = null; job = null; selectedIndex = null;
  $('result-area').hidden = true; $('empty').hidden = false;
  $('empty').querySelector('h2').textContent = '正在核查当前账单';
  $('empty').querySelector('p').textContent = '完成后显示本次费用与证据，请保持本地服务运行。';
}
async function submit(synthetic) {
  try {
    if (!config) throw Error('服务尚未就绪。');
    if (!synthetic && !$('entity').value.trim()) throw Error('请先填写当前记账主体。');
    if (!synthetic && !chosenFile) throw Error('请先选择账单文件。');
    if (!synthetic && chosenFile.size > 30 * 1024 * 1024) throw Error('文件不能超过 30 MB。');
    busy(true); clearResults();
    message(synthetic ? '正在加载合成演示…' : '正在处理，请保持页面与本地服务运行。');
    $('run-state').textContent = '处理中';
    let content = '';
    if (!synthetic) {
      const bytes = new Uint8Array(await chosenFile.arrayBuffer());
      const pieces = [];
      for (let i = 0; i < bytes.length; i += 32768) pieces.push(String.fromCharCode(...bytes.subarray(i, i + 32768)));
      content = btoa(pieces.join(''));
    }
    const response = await api('/api/jobs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity: synthetic ? 'B' : $('entity').value, synthetic,
        mode: $('mode').value, consent: $('consent').checked, filename: chosenFile?.name, content })
    });
    job = (await response.json()).id;
    await poll();
  } catch (error) {
    message(error.message); $('run-state').textContent = '未完成'; busy(false);
    $('empty').querySelector('h2').textContent = '本次处理未完成';
    $('empty').querySelector('p').textContent = '请检查上方提示后重新开始。';
  }
}
async function poll() {
  try {
    const data = await (await api('/api/jobs/' + job)).json();
    if (data.state === 'queued' || data.state === 'running') { setTimeout(poll, 1500); return; }
    busy(false);
    if (data.state === 'failed') throw Error(data.error || '处理失败');
    current = data; render(data);
    message('处理结束。请选择费用核对原文与复核原因。');
  } catch (error) {
    busy(false); message(error.message); $('run-state').textContent = '连接或处理失败';
    $('empty').querySelector('h2').textContent = '本次处理未完成';
    $('empty').querySelector('p').textContent = '请检查本地服务和上方提示。';
  }
}
$('upload-form').addEventListener('submit', event => { event.preventDefault(); submit(false); });
$('demo').addEventListener('click', () => submit(true));

function render(data) {
  $('empty').hidden = true; $('result-area').hidden = false;
  const report = data.report || {}, rows = data.results || [];
  $('run-state').textContent = states[report.status] || report.status || '处理结束';
  $('document-name').textContent = data.filename; $('document-name').title = data.filename;
  $('document-meta').textContent = `主体：${data.entity} · ${data.synthetic ? '合成示例 / 回放' : data.mode === 'remote' ? '指定 OCR' : '本地处理'}`;
  $('count').textContent = rows.length;
  const pending = rows.filter(row => row.operation === 'PENDING_REVIEW').length;
  $('pending').textContent = pending;
  $('evidence-count').textContent = (data.evidence || []).length;
  $('filter-all-count').textContent = rows.length; $('filter-review-count').textContent = pending;
  const notes = [];
  if (data.synthetic) notes.push('合成数据回放，仅用于演示，不代表模型或 OCR 识别结果。');
  if (!rows.length) notes.push('未生成费用候选。空结果不能证明没有账单，请查看运行报告。');
  if (report.parse_error_count || report.extraction_error || report.extraction_errors?.length || report.processing_error)
    notes.push('部分处理未完成，请查看运行报告。');
  $('notice').hidden = !notes.length; $('notice').textContent = notes.join(' ');
  $('report').textContent = JSON.stringify(report, null, 2); $('report-panel').close();
  filter = 'all'; selectedIndex = null; activeTab = 'evidence';
  renderTable();
}
function amount(value) {
  if (value === null || value === undefined) return '—';
  // Keep source numeric precision; never truncate monetary values for display.
  return Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 20 });
}
function renderTable() {
  if (!current) return;
  const rows = current.results || [];
  const visible = rows.map((row, index) => ({ row, index }))
    .filter(({ row }) => filter === 'all' || row.operation === 'PENDING_REVIEW');
  $('fees').replaceChildren();
  for (const { row, index } of visible) {
    const tr = node('tr', undefined, 'fee');
    tr.dataset.index = index; tr.tabIndex = 0;
    const description = node('td');
    const button = node('button', row.charge_details?.charge_item || '未命名费用');
    button.type = 'button'; description.append(button);
    const price = node('td', amount(row.charge_details?.amount), 'numeric fee-amount');
    const currency = node('td', row.charge_details?.currency || '—');
    const status = node('td');
    status.append(node('span', operations[row.operation] || row.operation,
      'badge ' + (row.operation === 'PENDING_REVIEW' ? 'review' : row.operation === 'CREATE' ? 'create' : '')));
    tr.append(description, price, currency, status);
    tr.addEventListener('click', () => select(index));
    tr.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(index); }
    });
    $('fees').append(tr);
  }
  for (const name of ['all', 'review']) {
    const button = $('filter-' + name);
    button.classList.toggle('active', filter === name);
    button.setAttribute('aria-pressed', String(filter === name));
  }
  $('no-fees').hidden = visible.length > 0;
  if (!visible.some(item => item.index === selectedIndex)) selectedIndex = visible[0]?.index ?? null;
  select(selectedIndex);
}
for (const name of ['all', 'review']) $('filter-' + name).addEventListener('click', () => { filter = name; renderTable(); });
for (const button of document.querySelectorAll('[data-tab]')) button.addEventListener('click', () => {
  activeTab = button.dataset.tab; select(selectedIndex);
});
function resetDetailPosition(details, index, tab) {
  const reset = () => {
    if (selectedIndex === index && activeTab === tab) details.scrollTo({ top: 0, left: 0, behavior: 'instant' });
  };
  reset();
  // Let focus and browser layout settle without retaining the previous evidence offset.
  requestAnimationFrame(() => requestAnimationFrame(reset));
}
function select(index) {
  selectedIndex = index;
  for (const element of $('fees').children) {
    const active = Number(element.dataset.index) === index;
    element.classList.toggle('active', active); element.setAttribute('aria-selected', String(active));
  }
  for (const button of document.querySelectorAll('[data-tab]')) {
    const active = button.dataset.tab === activeTab;
    button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active));
  }
  $('detail').setAttribute('aria-labelledby', 'tab-' + activeTab);
  const details = $('detail'); details.replaceChildren(); details.scrollTop = 0; $('review-reasons').scrollTop = 0;
  const row = index === null ? null : current?.results[index];
  $('review-reasons').replaceChildren(); $('review-reasons').hidden = !row;
  if (!row) {
    $('selected-index').textContent = '';
    details.append(node('p', '当前没有可选费用，仍可下载证据或查看运行报告。', 'blank')); return;
  }
  $('selected-index').textContent = `${index + 1} / ${current.results.length}`;
  const reasons = row.audit_trail?.review_reasons || [];
  $('review-reasons').append(node('h3', reasons.length ? '待复核原因' : '核查提示'));
  if (reasons.length) {
    const list = node('ul', undefined, 'reasons');
    reasons.forEach(reason => list.append(node('li', reasonText[reason] || reason)));
    $('review-reasons').append(list);
  } else $('review-reasons').append(node('p', '当前规则未标记复核原因，请核对原文后使用。'));
  if (activeTab === 'json') { details.append(node('pre', JSON.stringify(row, null, 2), 'json')); resetDetailPosition(details, index, activeTab); return; }
  if (activeTab === 'fields') {
    const facts = node('div', undefined, 'facts');
    for (const [label, value] of [['单据类型', row.document_info?.document_type], ['单据号', row.document_info?.reference_no],
      ['日期', row.document_info?.document_date], ['签发方', row.transaction_party?.issuer], ['接收方', row.transaction_party?.recipient],
      ['AR / AP', row.financial_attributes?.posting_type], ['付款方式', row.charge_details?.payment_term],
      ['数量', row.charge_details?.quantity], ['单价', row.charge_details?.unit_price],
      ['主提单', row.transport_info?.master_bills?.join(', ')], ['分提单', row.transport_info?.house_bills?.join(', ')],
      ['箱号', row.transport_info?.containers?.join(', ')]]) {
      const fact = node('div', undefined, 'fact'); fact.append(node('span', label), node('strong', value ?? '待确认'));
      if (value === '') fact.lastChild.textContent = '待确认';
      facts.append(fact);
    }
    details.append(facts); resetDetailPosition(details, index, activeTab); return;
  }
  const audit = row.audit_trail || {};
  const proofs = [...(audit.evidence || []), ...(audit.business_source_evidence || []), ...(audit.party_evidence || []),
    ...(audit.remote_ocr_source_evidence || []), ...(audit.conflict_evidence || [])];
  const conflictIds = new Set((audit.conflict_evidence || []).map(e => e.id));
  const seen = new Set();
  for (const evidence of proofs) {
    if (seen.has(evidence.id)) continue;
    seen.add(evidence.id);
    const conflict = conflictIds.has(evidence.id);
    const proof = node('div', undefined, 'proof' + (conflict ? ' conflict' : ''));
    proof.append(node('div', `${conflict ? '冲突证据 · ' : ''}${evidence.source || ''} · ${evidence.location || ''}`, 'proof-heading'), node('pre', evidence.text || ''));
    details.append(proof);
  }
  if (!seen.size) details.append(node('p', '没有绑定原文证据，请人工复核。', 'blank'));
  resetDetailPosition(details, index, activeTab);
}
async function download(name) {
  if (!current || !job) return;
  // Capture ownership before awaiting network, so a new upload cannot rename an old download.
  const identity = job, filename = current.filename.replace(/\.[^.]+$/, '') + '-' + name;
  try {
    const response = await api(`/api/jobs/${identity}/${name}`);
    const url = URL.createObjectURL(await response.blob());
    const anchor = node('a'); anchor.href = url; anchor.download = filename; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { message(error.message); }
}
$('download').addEventListener('click', () => download('result.json'));
$('download-evidence').addEventListener('click', () => download('evidence.json'));
$('download-report').addEventListener('click', () => download('run_report.json'));
$('show-report').addEventListener('click', () => {
  $('report-panel').showModal();
});
fetch('/api/config').then(response => response.json()).then(value => {
  config = value;
  if (config.remote_available) {
    const option = $('mode').options[1]; option.disabled = false; option.textContent = '指定 OCR + 临时 URL';
  }
}).catch(() => message('无法连接本地服务，请重新启动。'));

$('close-report').addEventListener('click', () => $('report-panel').close());
