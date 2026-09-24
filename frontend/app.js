'use strict';

/* ------------------------------------------------------------------ *
 * Фронтенд к пайплайну «созвон -> обязательства».
 * Ничего, чего нет в ответе API, здесь не додумывается: пустой владелец
 * остаётся пустым, нераскрытая дата остаётся текстом с пометкой.
 * ------------------------------------------------------------------ */

const POLL_MS = 1500;
const MAX_DURATION_SEC = 180;

const state = {
  file: null,
  fileUrl: null,
  fileDuration: null,
  jobId: null,
  data: null,
  pollTimer: null,
  startedAt: 0,
  elapsedTimer: null,
  lastPct: 0,
  lastStage: null,
};

const $ = (id) => document.getElementById(id);

/* ---------------------------- утилиты ---------------------------- */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function tc(sec) {
  if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return '—';
  const t = Math.max(0, Number(sec));
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function humanSize(bytes) {
  if (!bytes && bytes !== 0) return '—';
  const u = ['Б', 'КБ', 'МБ', 'ГБ'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}

function humanDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('uk-UA', { day: 'numeric', month: 'long', year: 'numeric' });
}

function showView(name) {
  for (const v of ['upload', 'progress', 'result']) {
    $(`view-${v}`).hidden = v !== name;
  }
  $('btn-restart').hidden = name === 'upload';
}

/* ------------------------- экран загрузки ------------------------- */

const dropzone = $('dropzone');
const fileInput = $('file-input');

$('btn-pick').addEventListener('click', (e) => { e.stopPropagation(); fileInput.click(); });
dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
});
fileInput.addEventListener('change', () => {
  if (fileInput.files && fileInput.files[0]) acceptFile(fileInput.files[0]);
});

['dragenter', 'dragover'].forEach((ev) => {
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropzone.classList.add('dropzone--hot');
  });
});
['dragleave', 'drop'].forEach((ev) => {
  dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    if (ev === 'dragleave' && dropzone.contains(e.relatedTarget)) return;
    dropzone.classList.remove('dropzone--hot');
  });
});
dropzone.addEventListener('drop', (e) => {
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) acceptFile(f);
});
// не даём браузеру открыть файл, если промахнулись мимо зоны
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => e.preventDefault());

const preview = $('preview');

function acceptFile(file) {
  const okType = /^(audio|video)\//.test(file.type) ||
    /\.(wav|mp3|m4a|ogg|opus|webm|mp4|flac|aac)$/i.test(file.name);
  if (!okType) {
    uploadError('Це не схоже на аудіо. Потрібен wav, mp3, m4a, ogg, webm або mp4.');
    return;
  }

  clearFile();                       // прошлый файл сносим целиком, вместе с плеером
  state.file = file;
  state.fileUrl = URL.createObjectURL(file);

  $('file-card').hidden = false;
  $('file-name').textContent = file.name;
  $('file-size').textContent = humanSize(file.size);

  // Показываем, какая дата уйдёт по умолчанию, но поле не заполняем:
  // заполненное = «указал человек», а человек ничего не указывал.
  $('recorded-at-hint').textContent = file.lastModified
    ? `не вкажете — візьмемо дату файлу: ${humanDate(new Date(file.lastModified).toISOString().slice(0, 10))}`
    : 'не вкажете — сервер візьме дату завантаження';

  // Предпрослушка и есть источник длительности: отдельный временный
  // audio-элемент ради метаданных больше не нужен.
  preview.src = state.fileUrl;
  preview.load();
}

preview.addEventListener('loadedmetadata', () => {
  const sec = Number.isFinite(preview.duration) ? preview.duration : null;
  state.fileDuration = sec;
  $('file-duration').textContent = sec
    ? `${tc(sec)} (${Math.round(sec)} с)`
    : 'тривалість не визначилась';

  if (sec && sec > MAX_DURATION_SEC) {
    const warn = $('file-warning');
    warn.hidden = false;
    warn.textContent = `Запис довший за 3 хвилини (${tc(sec)}). Пайплайн розрахований на короткі дзвінки — обробка триватиме довше, якість не гарантується.`;
  }
});

preview.addEventListener('error', () => {
  if (!state.fileUrl) return;        // src сняли сами при очистке — это не ошибка
  $('file-duration').textContent = 'тривалість не визначилась';
});

/* Полный сброс выбранного файла. Одна точка на кнопку «Прибрати», на выбор
   нового файла и на возврат с экрана результата — чтобы нигде не осталось
   ни играющей предпрослушки, ни живого objectURL, ни старого предупреждения. */
function clearFile() {
  preview.pause();
  preview.removeAttribute('src');
  preview.load();                    // отцепляем blob ДО revoke, иначе консоль ругается
  if (state.fileUrl) URL.revokeObjectURL(state.fileUrl);

  state.file = null;
  state.fileUrl = null;
  state.fileDuration = null;

  fileInput.value = '';              // без этого тот же файл повторно не выберется
  $('recorded-at').value = '';
  $('recorded-at-hint').textContent = 'не вкажете — візьмемо дату файлу';
  $('file-card').hidden = true;
  $('file-name').textContent = '—';
  $('file-size').textContent = '—';
  $('file-duration').textContent = 'тривалість визначається…';
  $('file-warning').hidden = true;
  $('file-warning').textContent = '';
  uploadError(null);
}

function uploadError(msg) {
  const el = $('upload-error');
  if (!msg) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.innerHTML = esc(msg);
}

$('btn-clear').addEventListener('click', clearFile);

$('btn-send').addEventListener('click', () => {
  if (!state.file) return;
  preview.pause();                   // иначе предпрослушка играет поверх обработки
  startJob(state.file);
});

$('btn-restart').addEventListener('click', () => {
  stopPolling();
  stopSegment();
  $('player-bar').hidden = true;
  $('audio').removeAttribute('src');
  $('audio').load();
  state.data = null; state.jobId = null; state.lastPct = 0; state.lastStage = null;
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  clearFile();
  showView('upload');
  checkHealth();
});

checkHealth();

/* Бэкенд поднимает faster-whisper и pyannote в фоне и до готовности отвечает
   models_ready: false. Предупреждаем заранее, иначе первый прогон выглядит
   как зависание. Провалился пинг — молчим: человек ещё ничего не просил. */
async function checkHealth() {
  try {
    const res = await fetch('/api/health', { cache: 'no-store' });
    if (!res.ok) return;
    const h = await res.json();
    if (h.ffmpeg === false) {
      uploadError('На сервері не знайдено ffmpeg — розпізнавання впаде. Потрібен ffmpeg у PATH.');
    }
  } catch (e) { /* бэкенда нет — узнаем при отправке файла */ }
}

/* --------------------------- запуск job --------------------------- */

async function startJob(file) {
  showView('progress');
  startElapsed();
  state.lastPct = 0;
  state.lastStage = null;
  setProgress({ stage: 'Надсилаю файл', done: 0, total: 0 });

  const fd = new FormData();
  fd.append('file', file, file.name);

  /* Точка отсчёта для относительных сроков. Дату файла на диске знает
     только браузер — сервер видит уже безымянный временный файл, у которого
     mtime равен моменту загрузки. Без неё «до вівторка» не раскрыть.
     recorded_at шлём ТОЛЬКО если человек выбрал дату сам: бэкенд пометит
     её source="manual", и подставлять туда догадку нельзя. */
  if (file.lastModified) {
    fd.append('client_file_date', new Date(file.lastModified).toISOString().slice(0, 10));
  }
  const manualDate = $('recorded-at').value;
  if (manualDate) fd.append('recorded_at', manualDate);

  try {
    const res = await fetch('/api/jobs', { method: 'POST', body: fd });
    if (!res.ok) {
      // 415 / 413 / 400 — сервер файл не принял. Это не падение бэкенда:
      // человеку нужен другой файл, а не повтор той же отправки.
      let detail = `сервер відповів ${res.status}`;
      try { const b = await res.json(); if (b && b.detail) detail = b.detail; } catch (e) { /* не json */ }
      if (res.status >= 400 && res.status < 500) { rejectedByServer(detail); return; }
      throw new Error(detail);
    }
    const body = await res.json();
    if (!body || !body.job_id) throw new Error('у відповіді немає job_id');
    state.jobId = body.job_id;
    // Кладём id в адрес: перезагрузка страницы или отправка ссылки коллеге
    // не должны стоить ещё одного прогона на несколько минут.
    location.hash = `job=${body.job_id}`;
    poll();
  } catch (err) {
    backendUnavailable(err);
  }
}

function rejectedByServer(detail) {
  stopPolling();
  stopElapsed();
  showView('upload');
  uploadError(`Файл не прийнято: ${detail}`);
}

function backendUnavailable(err) {
  stopPolling();
  stopElapsed();
  showView('upload');
  uploadError(`Бекенд недоступний: ${err && err.message ? err.message : err}. Перевірте, чи запущено сервер, і спробуйте ще раз.`);
}

async function poll() {
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(state.jobId)}`, { cache: 'no-store' });
    if (res.status === 404) {
      // Очередь живёт в памяти процесса: после рестарта сервера ссылка
      // на старую задачу мертва, и гонять по ней опрос бессмысленно.
      stopPolling(); stopElapsed();
      if (location.hash) history.replaceState(null, '', location.pathname + location.search);
      showView('upload');
      uploadError('Завдання не знайдено — сервер перезапускався. Завантажте файл заново.');
      return;
    }
    if (!res.ok) throw new Error(`статус ${res.status}`);
    const body = await res.json();

    if (body.status === 'processing' || body.status === 'queued' || body.status === 'pending') {
      setProgress(body.progress || {});
      state.pollTimer = setTimeout(poll, POLL_MS);
      return;
    }
    if (body.status === 'done') {
      stopPolling(); stopElapsed();
      renderResult(body);
      return;
    }
    // всё остальное считаем ошибкой обработки
    stopPolling(); stopElapsed();
    showView('upload');
    uploadError(`Обробка не вдалася: ${body.error || body.detail || body.status || 'причину не вказано'}`);
  } catch (err) {
    stopPolling(); stopElapsed();
    backendUnavailable(err);
  }
}

function stopPolling() {
  if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
}

/* ---------------------------- прогресс ---------------------------- */

/* Строки стадий приходят с бэкенда как есть (jobs.py, stt/local_whisper.py и
   vendor/transcriber.py): «Готую аудіо», «Розпізнаю мовлення», «Розшифровка
   готова», шаги pyannote («Шукаю мовлення (сегментація)», «Рахую відбитки
   голосів», …), «Витягую зобовʼязання», «Перевіряю цитати».
   Опознаём их регулярками и подсвечиваем нужный этап в списке. Не опознали —
   показываем строку бэкенда как есть: лучше чужая формулировка, чем
   выдуманный этап.
   Порядок проверки важен: диаризация идёт ПЕРЕД распознаванием, иначе
   «Шукаю мовлення (сегментація)» зацепится за «мовлен» и подсветит не тот
   этап. */
const WARMUP_RE = /завантажую модел|підіймаю модел|прогрів/i;
const STAGE_MATCH = [
  ['ffmpeg', /ffmpeg|готую аудіо|підготов|конверт|доріжк/i],
  ['diarization', /діариз|спікер|сегмент|відбитк|мовц|розмітк|голос|кластер|групу/i],
  ['stt', /розпізна|whisper|мовлен|транскри|розшифров/i],
  ['llm', /llm|витяг|зобов|deepseek/i],
  ['validation', /перевір|цитат|валід/i],
];

const STAGE_UA = {
  ffmpeg: 'Готую аудіо',
  diarization: 'Розділяю спікерів',
  stt: 'Розпізнаю мовлення',
  llm: 'Витягую зобов’язання',
  validation: 'Перевіряю цитати',
};

// Служебные состояния очереди — они мимо списка этапов.
// Третий элемент — своя строка пояснения вместо счётчика объёма.
const STAGE_SPECIAL_UA = [
  [/не розпізнано|немає мовлення/i, 'Мовлення не розпізнано', 'у записі не знайшлося жодної репліки'],
  [/у черзі|в черзі|черга/i, 'У черзі',
   'сервер обробляє одну задачу за раз — ця чекає, поки звільниться'],
  [/^готово/i, 'Готово', 'збираю результат'],
  [/помилк/i, 'Помилка', null],
];

function setProgress(p) {
  const stage = p && p.stage ? String(p.stage) : 'Обробляю';
  const done = Number(p && p.done);
  const total = Number(p && p.total);

  const warming = WARMUP_RE.test(stage);

  // Какой из пяти этапов сейчас идёт — по нему же берём украинский заголовок.
  let key = null;
  if (!warming) {
    for (const [k, re] of STAGE_MATCH) { if (re.test(stage)) { key = k; break; } }
  }

  let label = null;
  let detail = null;
  if (warming) {
    label = 'Завантажую моделі';
    detail = 'перший запуск: піднімаються faster-whisper і pyannote, це десятки секунд';
  } else {
    for (const [re, ua, det] of STAGE_SPECIAL_UA) {
      if (re.test(stage)) { label = ua; detail = det; key = null; break; }
    }
    if (!label && key) label = STAGE_UA[key];
  }
  $('progress-stage').textContent = label || stage;

  // Счётчик монотонен ВНУТРИ стадии и обнуляется при её смене: диаризация
  // шлёт свои под-шаги («сегментация», «отпечатки голосов», …) каждый со
  // своими 0-100%, и общий кламп намертво вешал полосу на максимуме.
  if (stage !== state.lastStage) { state.lastPct = 0; state.lastStage = stage; }

  let pct = null;
  if (Number.isFinite(done) && Number.isFinite(total) && total > 0) {
    pct = Math.max(0, Math.min(100, (done / total) * 100));
    pct = Math.max(pct, state.lastPct); // внутри стадии назад не отматываем
    state.lastPct = pct;
  }

  const bar = $('progress-bar');
  const track = $('progress-bar-el');
  if (pct === null) {
    $('progress-pct').textContent = '…';
    track.removeAttribute('aria-valuenow');
    track.classList.add('progress__track--indet');
    bar.style.width = '100%';
  } else {
    $('progress-pct').textContent = `${Math.round(pct)}%`;
    track.setAttribute('aria-valuenow', String(Math.round(pct)));
    track.classList.remove('progress__track--indet');
    bar.style.width = `${pct}%`;
  }

  /* У done/total единиц измерения нет — их смысл зависит от этапа. На
     распознавании это секунды записи (seg.end из faster-whisper), а на
     диаризации pyannote отдаёт НОМЕР ШАГА своего пайплайна (2 из 3).
     Форматировать вторые как таймкод нельзя: получалось «оброблено 0:02
     з 0:03 запису» вместо «крок 2 з 3». */
  if (detail === null) {
    const measurable = Number.isFinite(done) && Number.isFinite(total) && total > 0;
    detail = !measurable
      ? 'триває етап без вимірюваного обсягу'
      : key === 'stt'
        ? `оброблено ${tc(done)} з ${tc(total)} запису`
        : `крок ${Math.round(done)} з ${Math.round(total)}`;
  }
  $('progress-detail').textContent = detail;

  // подсветка текущего этапа в списке
  const items = Array.from($('stages').children);
  const idx = key ? items.findIndex((li) => li.dataset.stage === key) : -1;
  items.forEach((li, i) => {
    li.classList.toggle('stages__item--now', i === idx);
    li.classList.toggle('stages__item--done', idx > -1 && i < idx);
  });
}

function startElapsed() {
  state.startedAt = Date.now();
  stopElapsed();
  state.elapsedTimer = setInterval(() => {
    $('progress-elapsed').textContent = tc((Date.now() - state.startedAt) / 1000);
  }, 500);
}
function stopElapsed() {
  if (state.elapsedTimer) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
}

/* --------------------------- рендер результата --------------------------- */

const STATUS_RU = {
  agreed: 'прийнято',
  proposed_not_accepted: 'запропоновано, не прийнято',
  cancelled: 'скасовано',
};

const EVENT_RU = {
  proposed: 'запропоновано',
  accepted: 'прийнято',
  rejected: 'відхилено',
  owner_assigned: 'призначено власника',
  deadline_set: 'призначено строк',
  deadline_corrected: 'строк змінено',
  cancelled: 'скасовано',
};

function speakerName(id) {
  if (!id) return null;
  const p = (state.data && state.data.participants || []).find((x) => x.speaker_id === id);
  return p && p.name ? p.name : id;
}

function renderResult(data) {
  state.data = data;
  showView('result');
  setupPlayer(data);

  const commitments = Array.isArray(data.commitments) ? data.commitments : [];
  const verified = (c) => !(c.verification && c.verification.quote_verified === false);

  const agreed = commitments.filter((c) => verified(c) && c.status === 'agreed');
  const proposed = commitments.filter((c) => verified(c) && c.status === 'proposed_not_accepted');
  const cancelled = commitments.filter((c) => verified(c) && c.status === 'cancelled');

  // Бэкенд уносит непрошедшие валидатор пункты в отдельный список
  // unverified_commitments. Флаг quote_verified проверяем и в основном
  // списке — на случай, если что-то просочится мимо валидатора.
  const unverified = (Array.isArray(data.unverified_commitments) ? data.unverified_commitments : [])
    .concat(commitments.filter((c) => !verified(c)));

  const questions = Array.isArray(data.open_questions) ? data.open_questions : [];
  const clarifications = Array.isArray(data.clarifications_needed) ? data.clarifications_needed : [];

  renderHead(data, { agreed, proposed, cancelled, unverified, questions, clarifications });

  const host = $('sections');
  host.innerHTML = '';

  // Пайплайн дошёл до конца, но речи в файле не нашлось — это не пустой
  // результат «ничего не пообещали», и путать одно с другим нельзя.
  const nothingFound = !commitments.length && !unverified.length &&
    !questions.length && !clarifications.length;
  if (nothingFound) {
    const stage = (data.progress && data.progress.stage) || '';
    const noSpeech = /не розпізнано|немає мовлення/i.test(stage) ||
      !(Array.isArray(data.transcript) && data.transcript.length);
    const note = document.createElement('p');
    note.className = 'banner';
    note.textContent = noSpeech
      ? 'У записі не розпізнано жодної репліки. Перевірте, що файл не порожній, а мовлення українською.'
      : 'Мовлення розпізнано, але жодного зобов’язання, питання чи уточнення в розмові не прозвучало.';
    host.appendChild(note);
  }

  host.appendChild(section({
    mark: 'v', tone: 'ok',
    title: 'Прийняті зобов’язання',
    note: 'Фінальний стан розмови: прозвучала згода, і цитата підтверджена записом.',
    empty: 'Прийнятих зобов’язань у записі не знайдено.',
    items: agreed, render: commitmentCard,
  }));

  host.appendChild(section({
    mark: '?', tone: 'ask',
    title: 'Відкриті питання',
    note: 'Прозвучали, але відповіді в записі немає.',
    empty: 'Відкритих питань не зафіксовано.',
    items: questions, render: questionCard,
  }));

  host.appendChild(section({
    mark: '!', tone: 'warn',
    title: 'Потрібно уточнити',
    note: 'Даних не вистачило, і система не стала здогадуватися.',
    empty: 'Уточнень не потрібно.',
    items: clarifications, render: clarificationCard,
  }));

  host.appendChild(section({
    mark: '~', tone: 'muted',
    title: 'Запропоновано, але не прийнято',
    note: 'Завданнями не вважаються: згоди в записі не прозвучало.',
    empty: 'Неприйнятих пропозицій не знайдено.',
    items: proposed, render: commitmentCard,
  }));

  host.appendChild(section({
    mark: 'x', tone: 'dead',
    title: 'Скасовано',
    note: 'Було прийнято, але потім скасовано — до активного списку не потрапляє.',
    empty: 'Скасованих пунктів не знайдено.',
    items: cancelled, render: commitmentCard,
  }));

  host.appendChild(section({
    mark: '-', tone: 'unver',
    title: 'Не підтверджено записом',
    note: 'Цитата не знайшлася в транскрипті (quote_verified = false). Такі пункти виключені з активного списку — показані, щоб було видно, що система їх не загубила.',
    empty: 'Усі цитати підтверджені транскриптом.',
    items: unverified, render: commitmentCard,
  }));

  renderMetrics(data.metrics || {});
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderHead(data, groups) {
  const parts = Array.isArray(data.participants) ? data.participants : [];
  $('participants').innerHTML = `
    <div class="participants__list">
      ${parts.map((p, i) => `
        <span class="chip chip--sp${i % 2}">
          <span class="chip__id">${esc(p.speaker_id || '')}</span>
          <span class="chip__name">${esc(p.name || 'ім’я не назване')}</span>
        </span>`).join('') || '<span class="chip">спікерів не визначено</span>'}
    </div>
    <div class="participants__meta">
      ${esc(tc(data.duration_sec))} запису${data.language ? ` · мова: ${esc(data.language)}` : ''}
    </div>
    ${recordingDateHtml(data.recording_date)}`;

  const c = [
    ['Прийнято', groups.agreed.length, 'ok'],
    ['Питання', groups.questions.length, 'ask'],
    ['Уточнити', groups.clarifications.length, 'warn'],
    ['Не прийнято', groups.proposed.length, 'muted'],
    ['Скасовано', groups.cancelled.length, 'dead'],
    ['Без цитати', groups.unverified.length, 'unver'],
  ];
  $('counters').innerHTML = c.map(([label, n, tone]) => `
    <div class="counter counter--${tone}">
      <div class="counter__n">${n}</div>
      <div class="counter__l">${esc(label)}</div>
    </div>`).join('');
}

const DATE_SOURCE_UA = {
  manual: 'вказано вручну',
  file_metadata: 'з метаданих файлу',
  client_file_date: 'з дати файлу на диску',
  upload_time: 'за часом завантаження',
  unknown: 'джерело невідоме',
};

/* Точка отсчёта для относительных сроков. «До вівторка» имеет смысл только
   вместе с тем, от какого дня это посчитано и откуда день взялся, поэтому
   рядом с датой обязаны стоять источник и пояснение от бэкенда.
   Блока нет (старый бэкенд) — ничего не рисуем. */
function recordingDateHtml(rd) {
  if (!rd) return '';

  // Даты нет вообще — это нормальный исход, а не сбой: ни вручную, ни в
  // метаданных её не нашлось. Прятать такое нельзя, иначе непонятно, почему
  // сроки остались текстом «до вівторка».
  if (!rd.date) {
    return `
      <div class="recdate recdate--none">
        <span class="recdate__k">Дата запису</span>
        <b class="recdate__v none">не визначено</b>
        ${rd.detail ? `<div class="recdate__d">${esc(rd.detail)}</div>` : ''}
      </div>`;
  }

  return `
    <div class="recdate">
      <span class="recdate__k">Дата запису</span>
      <b class="recdate__v">${esc(humanDate(rd.date))}</b>
      <span class="recdate__s">${esc(DATE_SOURCE_UA[rd.source] || rd.source || '')}</span>
      ${rd.detail ? `<div class="recdate__d">${esc(rd.detail)}</div>` : ''}
    </div>`;
}

function section({ mark, tone, title, note, empty, items, render }) {
  const el = document.createElement('section');
  el.className = `sec sec--${tone}`;
  el.innerHTML = `
    <header class="sec__head">
      <span class="sec__mark">[${esc(mark)}]</span>
      <h2 class="sec__title">${esc(title)}</h2>
      <span class="sec__count">${items.length}</span>
    </header>
    <p class="sec__note">${esc(note)}</p>
    <div class="sec__body"></div>`;
  const body = el.querySelector('.sec__body');
  if (!items.length) {
    body.innerHTML = `<p class="sec__empty">${esc(empty)}</p>`;
  } else {
    items.forEach((item, i) => body.appendChild(render(item, i, tone)));
  }
  return el;
}

/* ------------------------------ карточки ------------------------------ */

function commitmentCard(c, i, tone) {
  const el = document.createElement('article');
  el.className = `card card--${tone}`;
  el.dataset.cardId = c.id || `c${i}`;

  const badges = [];
  if (c.owner === null || c.owner === undefined) {
    badges.push(badge('warn', 'власника не призначено', c.owner_missing_reason));
  }
  const d = c.deadline || {};
  if (d.resolution_status === 'relative_unresolved') {
    badges.push(badge('warn', 'дата не розкрита в записі', d.note));
  }
  if (c.verification && c.verification.quote_verified === false) {
    const v = c.verification;
    badges.push(badge('unver', 'цитату не знайдено в транскрипті',
      `збіг ${(Number(v.match_score) * 100 || 0).toFixed(0)}% — нижче порога, пункт виключено з активних`));
  }
  // timestamps_recomputed стоит почти всегда (таймкодам от модели веры нет,
  // их пересчитывает валидатор), поэтому это не бейдж-предупреждение,
  // а мелкая пометка в строке цитаты — иначе она шумит на каждой карточке.

  el.innerHTML = `
    <div class="card__top">
      <h3 class="card__title">${esc(c.title || 'без назви')}</h3>
      <span class="card__status">${esc(STATUS_RU[c.status] || c.status || '')}</span>
    </div>
    <dl class="facts">
      <div class="facts__row">
        <dt>Власник</dt>
        <dd>${ownerHtml(c)}</dd>
      </div>
      <div class="facts__row">
        <dt>Дедлайн</dt>
        <dd>${deadlineHtml(d)}</dd>
      </div>
    </dl>
    ${badges.length ? `<div class="badges">${badges.join('')}</div>` : ''}
    <div class="card__quote"></div>
    ${Array.isArray(c.lifecycle) && c.lifecycle.length ? lifecycleHtml(c) : ''}`;

  const q = c.primary_evidence || (c.lifecycle && c.lifecycle.length ? c.lifecycle[c.lifecycle.length - 1].evidence : null);
  el.querySelector('.card__quote').appendChild(
    quoteBlock(q, el, 'Підтверджувальна цитата', verificationNote(c.verification)));

  el.querySelectorAll('[data-ev]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      playEvidence(JSON.parse(btn.dataset.ev), btn, el);
    });
  });

  return el;
}

function ownerHtml(c) {
  const o = c.owner;
  if (!o || (!o.name && !o.speaker_id)) return `<span class="none">не призначено</span>`;

  // Владелец может быть опознан по голосу, но по имени в записи не назван.
  // Подставлять сюда кого-то нельзя — так и пишем.
  const who = o.name
    ? `<strong>${esc(o.name)}</strong>`
    : `<strong class="dim">ім’я в записі не назване</strong>`;
  const sp = o.speaker_id ? ` <span class="mono dim">(${esc(o.speaker_id)})</span>` : '';
  return `${who}${sp}${o.evidence ? miniPlay(o.evidence) : ''}`;
}

function deadlineHtml(d) {
  if (!d || (!d.raw_text && !d.resolved_date)) return `<span class="none">строк не названо</span>`;
  const bits = [];
  if (d.resolved_date && d.resolution_status === 'resolved') {
    bits.push(`<strong>${esc(humanDate(d.resolved_date))}</strong>`);
    if (d.raw_text) bits.push(`<span class="dim">«${esc(d.raw_text)}»</span>`);
  } else if (d.raw_text) {
    bits.push(`<strong>«${esc(d.raw_text)}»</strong>`);
  }
  if (d.evidence) bits.push(miniPlay(d.evidence));
  return bits.join(' ');
}

function badge(tone, text, note) {
  return `<span class="bdg bdg--${tone}">
    <span class="bdg__t">${esc(text)}</span>
    ${note ? `<span class="bdg__n">${esc(note)}</span>` : ''}
  </span>`;
}

function miniPlay(ev) {
  if (!ev || ev.start === null || ev.start === undefined) return '';
  return `<button type="button" class="mini" data-ev='${esc(JSON.stringify(ev))}'
    title="Прослухати: «${esc(ev.text || '')}»">&#9654; ${esc(tc(ev.start))}</button>`;
}

/* Итог работы валидатора цитат мелкой строкой: процент совпадения и то,
   что таймкод взят из транскрипта, а не со слов модели. */
function verificationNote(v) {
  if (!v) return null;
  const score = Number(v.match_score);
  const bits = [];
  if (v.quote_verified) bits.push(`цитату звірено${Number.isFinite(score) && score > 0 ? ` на ${Math.round(score * 100)}%` : ''}`);
  if (v.timestamps_recomputed) bits.push('таймкод із транскрипту');
  return bits.length ? bits.join(', ') : null;
}

function quoteBlock(ev, cardEl, label, note) {
  const wrap = document.createElement('div');
  if (!ev || !ev.text) {
    wrap.className = 'quote quote--none';
    wrap.textContent = 'Цитату не додано.';
    return wrap;
  }
  wrap.className = 'quote';
  wrap.innerHTML = `
    <button type="button" class="play" data-ev='${esc(JSON.stringify(ev))}' aria-label="Прослухати фрагмент">
      <span class="play__icon">&#9654;</span>
    </button>
    <div class="quote__body">
      <div class="quote__text">«${esc(ev.text)}»</div>
      <div class="quote__meta">
        <span class="quote__label">${esc(label)}</span>
        <span class="dot">·</span>
        <span>${esc(speakerName(ev.speaker_id) || 'спікера не визначено')}</span>
        <span class="dot">·</span>
        <span class="mono">${esc(tc(ev.start))}–${esc(tc(ev.end))}</span>
        ${note ? `<span class="dot">·</span><span class="quote__ver">${esc(note)}</span>` : ''}
      </div>
    </div>`;
  return wrap;
}

function lifecycleHtml(c) {
  const rows = c.lifecycle.map((step) => {
    const ev = step.evidence || {};
    return `<li class="life__row">
      <span class="life__ev life__ev--${esc(step.event || '')}">${esc(EVENT_RU[step.event] || step.event || '?')}</span>
      <span class="life__time mono">${esc(tc(ev.start))}</span>
      <span class="life__q">${ev.text ? `«${esc(ev.text)}»` : '<span class="dim">цитати немає</span>'}</span>
      ${ev.text ? miniPlay(ev) : ''}
    </li>`;
  }).join('');
  return `<details class="how">
    <summary>Як це вийшло <span class="how__n">${c.lifecycle.length} под.</span></summary>
    <ol class="life">${rows}</ol>
  </details>`;
}

function questionCard(q, i) {
  const el = document.createElement('article');
  el.className = 'card card--ask';
  el.innerHTML = `<h3 class="card__title card__title--q">${esc(q.question || q.text || 'питання')}</h3>
    <div class="card__quote"></div>`;
  el.querySelector('.card__quote').appendChild(quoteBlock(q.evidence, el, 'Де прозвучало'));
  el.querySelectorAll('[data-ev]').forEach((btn) => {
    btn.addEventListener('click', (e) => { e.preventDefault(); playEvidence(JSON.parse(btn.dataset.ev), btn, el); });
  });
  return el;
}

function clarificationCard(c, i) {
  const el = document.createElement('article');
  el.className = 'card card--warn';
  el.innerHTML = `<h3 class="card__title card__title--q">${esc(c.reason || 'причину не вказано')}</h3>
    <div class="card__quote"></div>`;
  el.querySelector('.card__quote').appendChild(quoteBlock(c.evidence, el, 'Підстава'));
  el.querySelectorAll('[data-ev]').forEach((btn) => {
    btn.addEventListener('click', (e) => { e.preventDefault(); playEvidence(JSON.parse(btn.dataset.ev), btn, el); });
  });
  return el;
}

/* ------------------------------- метрики ------------------------------- */

const TIMING_RU = {
  ffmpeg: 'Підготовка аудіо', stt: 'Розпізнавання мовлення', diarization: 'Розділення спікерів',
  llm: 'Витягування (LLM)', validation: 'Перевірка цитат', total: 'Разом',
};

function renderMetrics(m) {
  const t = m.timing_sec || {};
  const llm = m.llm || {};
  const cost = m.cost_usd || {};
  const total = Number(t.total) || Object.entries(t).filter(([k]) => k !== 'total')
    .reduce((s, [, v]) => s + (Number(v) || 0), 0);

  const rows = Object.keys(TIMING_RU)
    .filter((k) => k !== 'total' && t[k] !== undefined && t[k] !== null)
    .map((k) => {
      const v = Number(t[k]) || 0;
      const w = total > 0 ? (v / total) * 100 : 0;
      return `<div class="tm">
        <div class="tm__k">${esc(TIMING_RU[k])}</div>
        <div class="tm__bar"><span style="width:${w.toFixed(1)}%"></span></div>
        <div class="tm__v mono">${v.toFixed(1)} с</div>
      </div>`;
    }).join('');

  const usd = (v, digits) => (v === null || v === undefined || Number.isNaN(Number(v)))
    ? '—' : `$${Number(v).toFixed(digits === undefined ? 4 : digits)}`;

  $('metrics').innerHTML = `
    <header class="sec__head">
      <span class="sec__mark">[i]</span>
      <h2 class="sec__title">Метрики прогону</h2>
    </header>
    <div class="metrics__grid">
      <div class="panel">
        <h3 class="panel__t">Час за етапами</h3>
        ${rows || '<p class="sec__empty">Таймінги не прийшли.</p>'}
        <div class="tm tm--total">
          <div class="tm__k">${esc(TIMING_RU.total)}</div>
          <div class="tm__bar"><span style="width:100%"></span></div>
          <div class="tm__v mono">${total ? total.toFixed(1) : '—'} с</div>
        </div>
      </div>

      <div class="panel">
        <h3 class="panel__t">Модель і токени</h3>
        <div class="kv"><span>Модель</span><b class="mono">${esc(llm.model || '—')}</b></div>
        <div class="kv"><span>Вхід</span><b class="mono">${fmtNum(llm.input_tokens)}</b></div>
        <div class="kv"><span>Вихід</span><b class="mono">${fmtNum(llm.output_tokens)}</b></div>
        <div class="kv"><span>Ретраї</span><b class="mono">${fmtNum(llm.retries)}</b></div>
      </div>

      <div class="panel panel--cost">
        <h3 class="panel__t">Вартість</h3>
        ${costHtml(cost, usd)}
      </div>
    </div>
    ${Array.isArray(m.assumptions) && m.assumptions.length ? `
      <div class="panel panel--wide">
        <h3 class="panel__t">Припущення при підрахунку</h3>
        <ul class="assum">${m.assumptions.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>
      </div>` : ''}`;
}

function fmtNum(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  return Number(v).toLocaleString('uk-UA');
}

/* Две цифры стоимости: обработка и она же с арендой сервера.
 *
 * Одна цифра врёт: переменная стоимость STT — честный ноль, потому что
 * распознавание локальное, но сервер под модели стоит денег и в простое.
 * При 300 записях в месяц аренда — это большая часть полной стоимости,
 * поэтому показываем обе и тут же расписываем, откуда взялась вторая.
 *
 * Поля хостинга появились в cost_usd позже основных, и бэкенд могут
 * откатить, а мок и прод не обязаны совпадать по версии. Поэтому всё,
 * что про хостинг, рисуется только при живых числах — иначе панель
 * выглядит ровно как раньше, одной цифрой, без «$NaN» и пустых блоков.
 */
function costHtml(cost, usd) {
  const num = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };

  const perRun = num(cost.hosting_per_run);
  const perMonth = num(cost.hosting_per_month);
  const runsMonth = num(cost.runs_per_month);
  const totalHosted = num(cost.total_with_hosting);
  const minuteHosted = num(cost.per_audio_minute_with_hosting);

  const hasHosting = (perRun !== null && perRun > 0) || (totalHosted !== null && totalHosted > 0);
  // Бэкенд считает total_with_hosting сам; складываем руками, только если
  // он поле не прислал, а аренду за прогон — прислал.
  const hosted = (totalHosted !== null && totalHosted > 0)
    ? totalHosted
    : (hasHosting && num(cost.total) !== null ? num(cost.total) + perRun : null);

  // Расшифровка второй цифры: без неё она выглядит взятой с потолка.
  const calc = (hasHosting && perMonth !== null && perMonth > 0 && runsMonth !== null && runsMonth > 0)
    ? `${esc(cost.hosting_name || 'Оренда сервера')} · ${usd(perMonth, 2)}/міс ÷ ${fmtNum(runsMonth)} записів = ${usd(perRun)} на запис`
    : (hasHosting && cost.hosting_name ? `${esc(cost.hosting_name)} · ${usd(perRun)} на запис` : '');

  const minuteRows = (hasHosting && minuteHosted !== null && minuteHosted > 0)
    ? `<div class="kv"><span>Хвилина аудіо, обробка</span><b class="mono">${usd(cost.per_audio_minute)}</b></div>
       <div class="kv"><span>Хвилина аудіо, з орендою</span><b class="mono">${usd(minuteHosted)}</b></div>`
    : `<div class="kv"><span>За хвилину аудіо</span><b class="mono">${usd(cost.per_audio_minute)}</b></div>`;

  return `
    <div class="cost">
      <div class="cost__row">
        <span class="cost__v">${usd(cost.total)}</span>
        <span class="cost__l">${hasHosting ? 'обробка' : 'за прогін'}</span>
      </div>
      ${hasHosting && hosted !== null ? `
        <div class="cost__row cost__row--host">
          <span class="cost__v">${usd(hosted)}</span>
          <span class="cost__l">з орендою сервера</span>
        </div>` : ''}
      ${calc ? `<p class="cost__calc">${calc}</p>` : ''}
    </div>
    ${minuteRows}
    <div class="kv"><span>LLM</span><b class="mono">${usd(cost.llm)}</b></div>
    <div class="kv"><span>STT (локальний)</span><b class="mono">${usd(cost.stt, 2)}</b></div>
    ${hasHosting ? `<div class="kv"><span>Оренда на прогін</span><b class="mono">${usd(perRun)}</b></div>` : ''}`;
}

/* -------------------------------- плеер -------------------------------- */

const audio = $('audio');
const seg = { start: null, end: null, btn: null, card: null, raf: null };
let pauseGuard = 0;

function setupPlayer(data) {
  $('player-bar').hidden = false;
  // Аудио берём у бэкенда: после перезагрузки страницы по ссылке #job=<id>
  // локального файла у нас уже нет, а запись для плеера нужна.
  const src = data.audio_url || state.fileUrl;

  if (!src) {
    playerLabel('Аудіо не підключено',
      'Запис недоступний — цитати показані з таймкодами, але прослухати їх нічим.');
    return;
  }
  audio.src = src;
  audio.load();
  playerLabel('Плеєр готовий', 'Натисніть ▶ біля будь-якої цитати, щоб почути саме цей фрагмент.');

  audio.onerror = () => {
    if (state.fileUrl && audio.src !== state.fileUrl) {
      audio.src = state.fileUrl;  // бэкенд не отдал аудио — играем локальный файл
      audio.load();
    } else {
      playerLabel('Аудіо не завантажилось', 'Перевірте ендпоінт /api/jobs/{id}/audio — він має підтримувати Range-запити.');
    }
  };
}

function playerLabel(label, quote) {
  $('player-label').textContent = label;
  $('player-quote').textContent = quote;
}

function playEvidence(ev, btn, cardEl) {
  if (!ev || ev.start === null || ev.start === undefined) return;
  if (!audio.src) {
    playerLabel('Аудіо недоступне', 'Завантажте файл — тоді цитату можна буде прослухати.');
    return;
  }

  // повторный клик по той же кнопке — стоп
  if (seg.btn === btn && !audio.paused) { stopSegment(); return; }

  stopSegment();

  seg.start = Number(ev.start);
  seg.end = (typeof ev.end === 'number' && ev.end > ev.start) ? ev.end : seg.start + 8;
  seg.btn = btn;
  seg.card = cardEl || null;

  btn.classList.add('is-playing');
  if (seg.card) seg.card.classList.add('card--playing');
  $('btn-stop-segment').hidden = false;
  playerLabel(
    `${speakerName(ev.speaker_id) || 'спікер'} · ${tc(ev.start)}–${tc(ev.end)}`,
    `«${ev.text || ''}»`
  );

  seekAndPlay(Number(ev.start));
}

function seekAndPlay(t) {
  const go = () => {
    try { audio.currentTime = t; } catch (e) { /* источник ещё не готов к перемотке */ }
    const p = audio.play();
    if (p && p.catch) p.catch(() => {
      playerLabel('Не вдалося запустити відтворення', 'Натисніть play у плеєрі внизу — браузер вимагає прямої дії.');
      stopSegment();
    });
    watch();
  };
  if (audio.readyState >= 1) go();
  else audio.addEventListener('loadedmetadata', go, { once: true });
}

// Автостоп на evidence.end. timeupdate даёт всего ~4 события в секунду и
// перелетает границу фрагмента, поэтому сторожим через requestAnimationFrame.
function watch() {
  cancelAnimationFrame(seg.raf);
  const tick = () => {
    if (seg.end === null) return;
    if (audio.currentTime >= seg.end) { stopSegment(); return; }
    if (seg.card && seg.end > seg.start) {
      const done = (audio.currentTime - seg.start) / (seg.end - seg.start);
      seg.card.style.setProperty('--seg', `${Math.max(0, Math.min(1, done)) * 100}%`);
    }
    seg.raf = requestAnimationFrame(tick);
  };
  seg.raf = requestAnimationFrame(tick);
}

function stopSegment() {
  cancelAnimationFrame(seg.raf);
  if (!audio.paused) { pauseGuard++; audio.pause(); }
  if (seg.btn) seg.btn.classList.remove('is-playing');
  if (seg.card) { seg.card.classList.remove('card--playing'); seg.card.style.removeProperty('--seg'); }
  seg.start = null; seg.end = null; seg.btn = null; seg.card = null; seg.raf = null;
  $('btn-stop-segment').hidden = true;
}

$('btn-stop-segment').addEventListener('click', stopSegment);

// Событие pause прилетает асинхронно, в том числе от наших же вызовов —
// свои паузы гасим счётчиком, чтобы они не убивали только что начатый фрагмент.
audio.addEventListener('pause', () => {
  if (pauseGuard > 0) { pauseGuard--; return; }
  if (seg.btn) stopSegment();
});
audio.addEventListener('seeking', () => {
  if (seg.end !== null && (audio.currentTime > seg.end + 0.5)) stopSegment();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') stopSegment(); });

/* ------------------------- присутствие вкладки ------------------------- */

/* Сервис локальный: сервер нужен ровно пока открыта страница. Пока она жива,
   шлём сердцебиение; когда уходит — маячок.
   Решение о выключении принимает бэкенд: pagehide срабатывает и на обычной
   перезагрузке, так что маячок означает «страница ушла», а не «выключайся
   сейчас же» — сервер выжидает, не вернётся ли она.
   Ручек /api/alive и /api/bye может не быть (бэкенд их не обязан иметь) —
   тогда fetch просто молча падает, и всё работает как раньше. */
const ALIVE_MS = 5000;
let aliveTimer = null;

async function ping() {
  try {
    const res = await fetch('/api/alive', { method: 'POST', cache: 'no-store', keepalive: true });
    // Ручки нет — перестаём стучаться, иначе консоль и лог сервера забиваются
    // ошибками каждые пять секунд. Кодов несколько: FastAPI на незнакомый
    // путь отвечает 404, на неподходящий метод 405, а простой http.server —
    // вообще 501 Unsupported method.
    if ([404, 405, 501].includes(res.status) && aliveTimer) {
      clearInterval(aliveTimer);
      aliveTimer = null;
    }
  } catch (e) { /* сервер уже ушёл — не наша забота */ }
}
ping();
aliveTimer = setInterval(ping, ALIVE_MS);

window.addEventListener('pagehide', () => {
  if (navigator.sendBeacon) navigator.sendBeacon('/api/bye', '');
});

// В фоновой вкладке браузер душит таймеры до одного раза в минуту, поэтому
// при возврате отмечаемся сразу, не дожидаясь очередного интервала.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) ping();
});

/* ------------------------------ автостарт ------------------------------ */

resumeFromHash();

// Ссылку с #job=<id> могут открыть в уже открытой вкладке. Браузер при этом
// документ не перезагружает — меняется только хеш, и без этого обработчика
// страница просто ничего не делала бы.
window.addEventListener('hashchange', resumeFromHash);

// #job=<id> в адресе — подхватываем задачу, а не начинаем с нуля.
// Аудио для плеера при этом отдаёт бэкенд, так что работает всё, кроме
// локального файла (его после перезагрузки у страницы уже нет).
function resumeFromHash() {
  const m = /(?:^|[#&])job=([A-Za-z0-9_-]+)/.exec(location.hash || '');
  if (!m) return;
  // Хеш мы проставляем и сами при старте задачи — на свой же hashchange
  // второй опрос заводить не надо.
  if (m[1] === state.jobId) return;

  stopPolling();
  stopSegment();
  clearFile();
  state.jobId = m[1];
  showView('progress');
  setProgress({ stage: 'Забираю завдання', done: 0, total: 0 });
  startElapsed();
  poll();
}
