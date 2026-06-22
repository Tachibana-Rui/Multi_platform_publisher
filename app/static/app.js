const state = {
  posts: [],
  editingPost: null,
  pendingFiles: [],
  view: localStorage.getItem("content-hub-view") || "grid",
  scanPollTimer: null,
  adapterPost: null,
  platformVersion: null,
  adapterPlatform: "douyin",
  llmSettings: null,
  platformDirty: false,
  publication: null,
  publicationPollTimer: null,
  accounts: [],
  accountPollTimer: null,
  browserSyncedContent: null,
  assetMatches: [],
  importJob: null,
  importPollTimer: null,
  publishedPublications: [],
  queuePosts: [],
  queuePublications: [],
  queueFocusIds: [],
  queuePollTimer: null,
};

const elements = {
  grid: document.querySelector("#postGrid"),
  modal: document.querySelector("#editorModal"),
  form: document.querySelector("#postForm"),
  title: document.querySelector("#titleInput"),
  body: document.querySelector("#bodyInput"),
  bodyCount: document.querySelector("#bodyCount"),
  tags: document.querySelector("#tagsInput"),
  status: document.querySelector("#postStatus"),
  source: document.querySelector("#sourcePlatform"),
  sourceUrl: document.querySelector("#sourceUrl"),
  fileInput: document.querySelector("#fileInput"),
  dropzone: document.querySelector("#dropzone"),
  assets: document.querySelector("#assetPreviewList"),
  save: document.querySelector("#saveButton"),
  deletePost: document.querySelector("#deletePostButton"),
  search: document.querySelector("#searchInput"),
  statusFilter: document.querySelector("#statusFilter"),
  typeFilter: document.querySelector("#typeFilter"),
  importModal: document.querySelector("#importModal"),
  importForm: document.querySelector("#importForm"),
  importUrl: document.querySelector("#importUrl"),
  importPlatform: document.querySelector("#importPlatform"),
  importPlatformMark: document.querySelector("#importPlatformMark"),
  importPlatformName: document.querySelector("#importPlatformName"),
  batchImportResults: document.querySelector("#batchImportResults"),
  batchImportProgress: document.querySelector("#batchImportProgress"),
  batchImportProgressBar: document.querySelector("#batchImportProgressBar"),
  batchImportProgressText: document.querySelector("#batchImportProgressText"),
  confirmRights: document.querySelector("#confirmRights"),
  startImport: document.querySelector("#startImportButton"),
  libraryModal: document.querySelector("#libraryModal"),
  pickRootButton: document.querySelector("#pickRootButton"),
  rootList: document.querySelector("#rootList"),
  librarySearchForm: document.querySelector("#librarySearchForm"),
  libraryResults: document.querySelector("#libraryResults"),
  matchActions: document.querySelector("#matchActions"),
  matchButton: document.querySelector("#matchOriginalButton"),
  manualMatchButton: document.querySelector("#manualMatchButton"),
  openPlatformButton: document.querySelector("#openPlatformButton"),
  aiConfigModal: document.querySelector("#aiConfigModal"),
  aiConfigForm: document.querySelector("#aiConfigForm"),
  doubaoApiKey: document.querySelector("#doubaoApiKey"),
  doubaoModel: document.querySelector("#doubaoModel"),
  apiKeyState: document.querySelector("#apiKeyState"),
  apiKeyHint: document.querySelector("#apiKeyHint"),
  storageModal: document.querySelector("#storageModal"),
  storagePath: document.querySelector("#storagePath"),
  storageStats: document.querySelector("#storageStats"),
  pickStorage: document.querySelector("#pickStorageButton"),
  saveStorage: document.querySelector("#saveStorageButton"),
  accountModal: document.querySelector("#accountModal"),
  accountGrid: document.querySelector("#accountGrid"),
  refreshAccounts: document.querySelector("#refreshAccountsButton"),
  platformModal: document.querySelector("#platformModal"),
  targetPlatform: document.querySelector("#targetPlatform"),
  publicationVisibility: document.querySelector("#publicationVisibility"),
  platformTitle: document.querySelector("#platformTitle"),
  platformBody: document.querySelector("#platformBody"),
  generationPrompt: document.querySelector("#generationPrompt"),
  generateCopy: document.querySelector("#generateCopyButton"),
  platformAssets: document.querySelector("#platformAssetsGrid"),
  selectedImageCount: document.querySelector("#selectedImageCount"),
  copySourceBadge: document.querySelector("#copySourceBadge"),
  llmReadiness: document.querySelector("#llmReadiness"),
  versionMeta: document.querySelector("#versionMeta"),
  savePlatform: document.querySelector("#savePlatformButton"),
  preparePublish: document.querySelector("#preparePublishButton"),
  publicationPanel: document.querySelector("#publicationPanel"),
  imageViewer: document.querySelector("#imageViewer"),
  imageViewerImage: document.querySelector("#imageViewerImage"),
  imageViewerCaption: document.querySelector("#imageViewerCaption"),
  platformManagerModal: document.querySelector("#platformManagerModal"),
  platformManagerFilter: document.querySelector("#platformManagerFilter"),
  platformManagerSummary: document.querySelector("#platformManagerSummary"),
  publishedGrid: document.querySelector("#publishedGrid"),
  publishQueueModal: document.querySelector("#publishQueueModal"),
  queuePostSelect: document.querySelector("#queuePostSelect"),
  queueVisibility: document.querySelector("#queueVisibility"),
  startBatchPublish: document.querySelector("#startBatchPublishButton"),
  queueProgressBar: document.querySelector("#queueProgressBar"),
  queueProgressValue: document.querySelector("#queueProgressValue"),
  queueProgressText: document.querySelector("#queueProgressText"),
  queueList: document.querySelector("#queueList"),
};

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");

const formatBytes = (bytes) => {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
};

const formatDate = (value) => new Intl.DateTimeFormat("zh-CN", {
  month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
}).format(new Date(value));

const statusLabel = { draft: "草稿", ready: "就绪", archived: "已归档" };

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      message = Array.isArray(payload.detail)
        ? payload.detail.map((item) => item.msg).join("；")
        : payload.detail || message;
    } catch { /* use the fallback */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, isError = false) {
  const node = document.createElement("div");
  node.className = `toast${isError ? " error" : ""}`;
  node.textContent = message;
  document.querySelector("#toastRegion").append(node);
  window.setTimeout(() => node.remove(), 3200);
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  document.querySelector("#totalPosts").textContent = data.total_posts;
  document.querySelector("#draftPosts").textContent = data.draft_posts;
  document.querySelector("#readyPosts").textContent = data.ready_posts;
  document.querySelector("#assetCount").textContent = data.total_assets;
  document.querySelector("#assetSize").textContent = `共占用 ${formatBytes(data.total_bytes)}`;
}

async function loadPosts() {
  elements.grid.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  const params = new URLSearchParams();
  if (elements.search.value.trim()) params.set("search", elements.search.value.trim());
  if (elements.statusFilter.value) params.set("status", elements.statusFilter.value);
  if (elements.typeFilter.value) params.set("content_type", elements.typeFilter.value);
  state.posts = await api(`/api/posts?${params}`);
  renderPosts();
}

function coverMarkup(post) {
  const asset = post.assets[0];
  if (!asset) return '<span class="cover-placeholder">✦</span>';
  if (asset.media_type === "image") return `<img src="${asset.url}" alt="" loading="lazy">`;
  return `<video src="${asset.url}#t=0.1" preload="metadata" muted></video>`;
}

function renderPosts() {
  document.querySelector("#resultSummary").textContent = state.posts.length
    ? `找到 ${state.posts.length} 份内容，最近更新优先`
    : "还没有符合条件的内容";
  elements.grid.classList.toggle("list-view", state.view === "list");
  if (!state.posts.length) {
    elements.grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-illustration">✦</div>
        <h3>从第一份原创内容开始</h3>
        <p>收好原始文案和素材，之后再放心地适配各个平台。</p>
        <button class="primary-button" type="button" data-create>新建内容</button>
      </div>`;
    elements.grid.querySelector("[data-create]").addEventListener("click", () => openEditor());
    return;
  }
  elements.grid.innerHTML = state.posts.map((post) => `
    <article class="post-card" data-post-id="${post.id}" tabindex="0">
      <div class="post-cover">
        ${coverMarkup(post)}
        ${post.assets.length ? `<span class="asset-badge">${post.assets.length} 个素材</span>` : ""}
      </div>
      <div class="post-card-body">
        <div class="card-meta">
          <span class="status-pill status-${post.status}">${statusLabel[post.status] || post.status}</span>
          <span class="card-date">${formatDate(post.updated_at)}</span>
        </div>
        <h3>${escapeHtml(post.title || "未命名内容")}</h3>
        <p class="post-excerpt">${escapeHtml(post.body || "暂无正文，点击继续编辑")}</p>
        <div class="tag-row">${post.tags.slice(0, 4).map((tag) => `<span class="tag">#${escapeHtml(tag)}</span>`).join("")}</div>
      </div>
    </article>`).join("");
  elements.grid.querySelectorAll("[data-post-id]").forEach((card) => {
    const open = () => openEditor(state.posts.find((item) => item.id === card.dataset.postId));
    card.addEventListener("click", open);
    card.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
  });
}

function openEditor(post = null) {
  state.editingPost = post;
  state.pendingFiles = [];
  state.assetMatches = [];
  elements.form.reset();
  document.querySelector("#modalTitle").textContent = post ? "编辑内容" : "新建内容";
  elements.deletePost.hidden = !post;
  if (post) {
    elements.title.value = post.title;
    elements.body.value = post.body;
    elements.tags.value = post.tags.join(", ");
    elements.status.value = post.status;
    elements.source.value = post.source_platform;
    elements.sourceUrl.value = post.source_url || "";
  }
  elements.bodyCount.textContent = elements.body.value.length;
  renderAssets();
  elements.matchActions.hidden = !post || !post.assets.some((asset) => asset.media_type === "image");
  elements.manualMatchButton.hidden = true;
  elements.openPlatformButton.hidden = !post;
  if (post) loadMatches(post.id).catch((error) => toast(error.message, true));
  elements.modal.hidden = false;
  document.body.style.overflow = "hidden";
  window.setTimeout(() => elements.title.focus(), 30);
}

function closeEditor() {
  elements.modal.hidden = true;
  document.body.style.overflow = "";
  state.pendingFiles = [];
}

function openImport() {
  if (!state.importJob || ["completed", "failed"].includes(state.importJob.status)) {
    elements.importForm.reset();
    elements.batchImportProgress.hidden = true;
    elements.batchImportProgressBar.style.width = "0%";
    elements.batchImportResults.hidden = true;
    elements.batchImportResults.innerHTML = "";
  }
  renderImportPlatform();
  elements.importModal.hidden = false;
  document.body.style.overflow = "hidden";
  window.setTimeout(() => elements.importUrl.focus(), 30);
}

function closeImport() {
  elements.importModal.hidden = true;
  document.body.style.overflow = "";
}

async function openLibrary() {
  elements.libraryModal.hidden = false;
  document.body.style.overflow = "hidden";
  await Promise.all([loadRoots(), searchLibrary()]);
}

function closeLibrary() {
  elements.libraryModal.hidden = true;
  document.body.style.overflow = "";
}

async function loadLlmSettings() {
  state.llmSettings = await api("/api/settings/llm");
  elements.doubaoModel.value = state.llmSettings.model;
  elements.doubaoApiKey.value = "";
  elements.apiKeyState.textContent = state.llmSettings.has_api_key ? "已配置" : "未配置";
  elements.apiKeyState.classList.toggle("ready", state.llmSettings.has_api_key);
  elements.apiKeyHint.textContent = state.llmSettings.has_api_key
    ? `当前密钥 ${state.llmSettings.api_key_hint}；留空将保留现有密钥。`
    : "保存后密钥将由 Windows 加密。";
  renderLlmReadiness();
  return state.llmSettings;
}

async function openAiConfig() {
  elements.aiConfigModal.hidden = false;
  document.body.style.overflow = "hidden";
  await loadLlmSettings();
  window.setTimeout(() => elements.doubaoApiKey.focus(), 30);
}

function closeAiConfig() {
  elements.aiConfigModal.hidden = true;
  document.body.style.overflow = "";
}

async function openStorageSettings() {
  elements.storageModal.hidden = false;
  document.body.style.overflow = "hidden";
  const storage = await api("/api/settings/storage");
  elements.storagePath.value = storage.path;
  elements.storageStats.textContent = `${storage.file_count} 个文件 · ${formatBytes(storage.total_bytes)}`;
  elements.storagePath.disabled = storage.environment_override;
  elements.pickStorage.disabled = storage.environment_override;
  elements.saveStorage.disabled = storage.environment_override;
  if (storage.environment_override) {
    elements.storageStats.textContent += " · 由环境变量控制";
  }
}

function closeStorageSettings() {
  elements.storageModal.hidden = true;
  document.body.style.overflow = "";
}

const accountStatusLabels = {
  unknown: "尚未检测", checking: "检测中", awaiting_login: "等待登录", logged_in: "已登录",
  not_logged_in: "未登录", busy: "平台忙碌", error: "检测失败",
};
const accountIcons = { douyin: "♪", xiaohongshu: "小红书", bilibili: "B" };

function renderAccounts(accounts) {
  state.accounts = accounts;
  elements.accountGrid.innerHTML = accounts.map((account) => {
    const working = ["checking", "awaiting_login", "busy"].includes(account.status);
    const action = account.status === "logged_in"
      ? '<span class="account-ready-mark">✓ 可以发布</span>'
      : `<button class="account-login-button" data-login-platform="${account.platform}" ${working ? "disabled" : ""}>${account.status === "awaiting_login" ? "请在浏览器登录" : "打开登录网页"}</button>`;
    return `<article class="account-card">
      <span class="account-platform-icon ${account.platform}">${accountIcons[account.platform]}</span>
      <div class="account-copy"><strong>${escapeHtml(account.name)}</strong><small>${escapeHtml(account.message)}</small></div>
      <span class="account-state ${account.status}">${accountStatusLabels[account.status] || account.status}</span>
      <div class="account-card-action">${action}</div>
    </article>`;
  }).join("");
}

async function loadAccounts() {
  clearTimeout(state.accountPollTimer);
  const accounts = await api("/api/accounts");
  renderAccounts(accounts);
  if (accounts.some((account) => ["checking", "awaiting_login"].includes(account.status))) {
    state.accountPollTimer = window.setTimeout(() => loadAccounts().catch((error) => toast(error.message, true)), 1200);
  }
  return accounts;
}

async function checkAccounts() {
  elements.refreshAccounts.disabled = true;
  try {
    await api("/api/accounts/check", { method: "POST" });
    await loadAccounts();
  } finally {
    elements.refreshAccounts.disabled = false;
  }
}

async function openAccountManager() {
  elements.accountModal.hidden = false;
  document.body.style.overflow = "hidden";
  await loadAccounts();
  await checkAccounts();
}

function closeAccountManager() {
  clearTimeout(state.accountPollTimer);
  elements.accountModal.hidden = true;
  document.body.style.overflow = "";
}

const platformLabels = {
  douyin: "抖音", xiaohongshu: "小红书", bilibili: "B站",
  kuaishou: "快手", wechat_channels: "视频号", wechat_moments: "微信朋友圈",
};

function selectedPlatformAssetIds() {
  return [...elements.platformAssets.querySelectorAll("input:checked")].map((input) => input.value);
}

function suggestedPrompt() {
  const selected = new Set(selectedPlatformAssetIds());
  const count = Math.min([...elements.platformAssets.querySelectorAll("input[data-media='image']")]
    .filter((input) => selected.has(input.value)).length, 4);
  const base = state.platformVersion?.suggested_prompt
    || `生成 cos作品 ${platformLabels[state.adapterPlatform]} 标题和文案，并生成5个相关标签追加在正文末尾。`;
  return base.replace(/带有\d+张图片/, `带有${count}张图片`);
}

function updateSelectedImageCount() {
  const selected = selectedPlatformAssetIds();
  elements.selectedImageCount.textContent = `已选择 ${selected.length} 个素材`;
  elements.platformAssets.querySelectorAll(".selectable-asset").forEach((label) => {
    label.classList.toggle("selected", label.querySelector("input").checked);
  });
}

function renderLlmReadiness() {
  if (!elements.llmReadiness) return;
  const ready = state.llmSettings?.has_api_key;
  elements.llmReadiness.className = `llm-readiness ${ready ? "ready" : "warning"}`;
  elements.llmReadiness.innerHTML = ready
    ? `<span></span><p>${escapeHtml(state.llmSettings.model)} 已就绪</p>`
    : '<span></span><p>尚未配置豆包 API Key</p>';
  elements.generateCopy.disabled = !ready;
}

function renderPlatformVersion(version) {
  state.platformVersion = version;
  elements.platformTitle.value = version.title;
  elements.platformBody.value = version.body;
  elements.copySourceBadge.textContent = version.content_source === "llm"
    ? "LLM 生成" : version.content_source === "manual"
      ? "人工编辑" : version.content_source === "browser" ? "平台页同步" : "原文复制";
  elements.copySourceBadge.classList.toggle("llm", version.content_source === "llm");
  const selected = new Set(version.selected_asset_ids);
  elements.platformAssets.innerHTML = version.assets.length ? version.assets.map((asset, index) => `
    <label class="selectable-asset ${selected.has(asset.id) ? "selected" : ""}">
      <input type="checkbox" data-media="${asset.media_type}" value="${asset.id}" ${selected.has(asset.id) ? "checked" : ""}>
      ${asset.media_type === "image"
        ? `<img src="${asset.url}" alt="${escapeHtml(asset.original_name)}" loading="lazy">`
        : `<span class="video-asset-preview"><b>▶</b><small>${escapeHtml(asset.original_name)}</small></span>`}
      <span class="asset-selection-mark">✓</span>
      <span class="asset-selection-index">${index + 1}</span>
    </label>`).join("") : '<div class="library-empty">这份内容还没有可选择的素材</div>';
  elements.platformAssets.querySelectorAll("input").forEach((input) => input.addEventListener("change", () => {
    state.platformDirty = true;
    updateSelectedImageCount();
    if (!state.platformVersion.last_prompt) elements.generationPrompt.value = suggestedPrompt();
  }));
  updateSelectedImageCount();
  elements.generationPrompt.value = version.last_prompt || suggestedPrompt();
  elements.generateCopy.textContent = version.generation_count > 0 ? "✦ 再次生成" : "✦ 一键生成";
  elements.versionMeta.textContent = version.generation_count
    ? `已调用 LLM ${version.generation_count} 次 · ${version.last_model || "模型未知"}`
    : "尚未调用 LLM，标题和正文来自 Content Hub 原稿";
  state.platformDirty = false;
  renderLlmReadiness();
}

async function loadPlatformVersion(platform) {
  state.adapterPlatform = platform;
  elements.targetPlatform.value = platform;
  const version = await api(`/api/posts/${state.adapterPost.id}/platform-versions/${platform}`);
  renderPlatformVersion(version);
  await loadLatestPublication();
}

const publicationStatusLabels = {
  pending: "等待启动", validating: "正在校验", queued: "排队中", awaiting_login: "等待登录",
  preparing: "正在上传和填写", review_pending: "等待最终确认", publishing: "正在发布",
  submitted: "已提交平台", published: "发布成功", failed: "发布失败", cancelled: "已取消",
};

function publicationCover(publication, className) {
  if (!publication.cover_url) return `<div class="${className}">✦</div>`;
  if (publication.cover_media_type === "image") {
    return `<div class="${className}"><img src="${publication.cover_url}" alt="" loading="lazy"></div>`;
  }
  return `<div class="${className}"><video src="${publication.cover_url}#t=0.1" muted preload="metadata"></video></div>`;
}

function renderPublishedWorks() {
  const platform = elements.platformManagerFilter.value;
  const items = state.publishedPublications.filter((item) => !platform || item.platform === platform);
  elements.platformManagerSummary.textContent = items.length
    ? `共 ${items.length} 份作品，按实际发布时间倒序`
    : "该平台还没有已发布作品";
  elements.publishedGrid.innerHTML = items.length ? items.map((item) => `
    <article class="published-card">
      ${publicationCover(item, "published-cover")}
      <div class="published-card-body">
        <div class="published-card-head"><span class="platform-pill">${platformLabels[item.platform] || item.platform}</span><time>${formatDate(item.published_at || item.updated_at)}</time></div>
        <h3 title="${escapeHtml(item.title || item.post_title)}">${escapeHtml(item.title || item.post_title || "未命名内容")}</h3>
        <p>${escapeHtml(item.body || "暂无平台正文")}</p>
        <div class="published-card-footer"><span>${item.status === "published" ? "发布成功" : "已提交平台"}</span>${item.platform_url ? `<a href="${escapeHtml(item.platform_url)}" target="_blank" rel="noreferrer">查看作品 ↗</a>` : ""}</div>
      </div>
    </article>`).join("") : '<div class="library-empty">完成一次平台发布后，作品会按平台出现在这里。</div>';
}

async function openPlatformManager() {
  elements.platformManagerModal.hidden = false;
  document.body.style.overflow = "hidden";
  const publications = await api("/api/publications");
  state.publishedPublications = publications.filter((item) => ["published", "submitted"].includes(item.status));
  renderPublishedWorks();
}

function closePlatformManager() {
  elements.platformManagerModal.hidden = true;
  document.body.style.overflow = "";
}

function queueActionMarkup(publication) {
  if (publication.status === "review_pending") {
    return '<button data-queue-action="confirm">确认发布</button>';
  }
  if (["failed", "cancelled"].includes(publication.status)) {
    return '<button data-queue-action="retry">重试</button>';
  }
  if (["pending", "validating", "queued", "awaiting_login", "preparing"].includes(publication.status)) {
    return '<button data-queue-action="cancel">取消</button>';
  }
  return "";
}

function queueVisiblePublications() {
  if (state.queueFocusIds.length) {
    const focus = new Set(state.queueFocusIds);
    return state.queuePublications.filter((item) => focus.has(item.id));
  }
  const active = state.queuePublications.filter((item) =>
    ["pending", "validating", "queued", "awaiting_login", "preparing", "review_pending", "publishing"].includes(item.status));
  return active.length ? active : state.queuePublications.slice(0, 12);
}

function renderPublishQueue() {
  const items = queueVisiblePublications();
  const progress = items.length
    ? Math.round(items.reduce((sum, item) => sum + (item.progress || 0), 0) / items.length)
    : 0;
  elements.queueProgressBar.style.width = `${progress}%`;
  elements.queueProgressValue.textContent = `${progress}%`;
  const progressTrack = elements.queueProgressBar.parentElement;
  progressTrack.setAttribute("aria-valuenow", String(progress));
  const terminal = items.filter((item) => ["submitted", "published", "failed", "cancelled"].includes(item.status)).length;
  const reviews = items.filter((item) => item.status === "review_pending").length;
  elements.queueProgressText.textContent = items.length
    ? `${terminal}/${items.length} 个任务已结束${reviews ? ` · ${reviews} 个等待人工确认` : ""}`
    : "暂无发布任务";
  elements.queueList.innerHTML = items.length ? items.map((item) => {
    const latestLog = item.logs?.at(-1);
    return `<article class="queue-item" data-publication-id="${item.id}">
      ${publicationCover(item, "queue-item-cover")}
      <div class="queue-item-copy"><strong>${escapeHtml(item.post_title || item.title || "未命名内容")} · ${platformLabels[item.platform] || item.platform}</strong><small>${escapeHtml(item.error_message || latestLog?.message || publicationStatusLabels[item.status] || item.status)}</small><div class="queue-item-progress"><span style="width:${item.progress || 0}%"></span></div></div>
      <div class="queue-item-side"><span class="publication-status ${item.status}">${publicationStatusLabels[item.status] || item.status}</span><div class="queue-item-actions">${queueActionMarkup(item)}</div></div>
    </article>`;
  }).join("") : '<div class="library-empty">选择本地内容和多个平台，即可创建批量发布任务。</div>';
}

async function loadPublishQueue() {
  clearTimeout(state.queuePollTimer);
  state.queuePublications = await api("/api/publications");
  renderPublishQueue();
  const items = queueVisiblePublications();
  if (items.some((item) => ["pending", "validating", "queued", "awaiting_login", "preparing", "review_pending", "publishing"].includes(item.status))) {
    state.queuePollTimer = window.setTimeout(
      () => loadPublishQueue().catch((error) => toast(error.message, true)), 1200,
    );
  }
}

async function openPublishQueue() {
  elements.publishQueueModal.hidden = false;
  document.body.style.overflow = "hidden";
  state.queuePosts = await api("/api/posts");
  const publishable = state.queuePosts.filter((post) => post.assets.length);
  elements.queuePostSelect.innerHTML = publishable.length
    ? publishable.map((post) => `<option value="${post.id}">${escapeHtml(post.title || "未命名内容")} · ${post.assets.length} 个素材</option>`).join("")
    : '<option value="">请先为内容添加素材</option>';
  elements.startBatchPublish.disabled = !publishable.length;
  await loadPublishQueue();
}

function closePublishQueue() {
  clearTimeout(state.queuePollTimer);
  state.queueFocusIds = [];
  elements.publishQueueModal.hidden = true;
  document.body.style.overflow = "";
}

function renderPublication(publication) {
  state.publication = publication;
  if (!publication) {
    elements.publicationPanel.innerHTML = `
      <div class="publication-heading"><strong>发布 Agent</strong><span>尚无任务</span></div>
      <p>点击“准备发布”后会打开独立平台窗口。首次使用请手动登录，Agent 不读取或保存账号密码。</p>`;
    return;
  }
  const latestLog = publication.logs?.at(-1);
  const hasBrowserSync = publication.logs?.some((item) => item.status === "content_synced");
  if (hasBrowserSync && state.platformVersion && !state.platformDirty) {
    const syncKey = `${publication.id}:${publication.title}:${publication.body}`;
    if (elements.platformTitle.value !== publication.title || elements.platformBody.value !== publication.body) {
      elements.platformTitle.value = publication.title;
      elements.platformBody.value = publication.body;
      state.platformVersion.title = publication.title;
      state.platformVersion.body = publication.body;
      state.platformVersion.content_source = "browser";
      elements.copySourceBadge.textContent = "平台页同步";
      if (state.browserSyncedContent !== syncKey) toast("已同步平台发布页中的文案修改");
    }
    state.browserSyncedContent = syncKey;
  }
  const visibilityLabels = { public: "公开可见", friends: "仅互关好友可见", private: "仅自己可见" };
  const validationErrors = (publication.validation || []).filter((item) => item.level === "error");
  const active = ["pending", "validating", "queued", "awaiting_login", "preparing", "review_pending", "publishing"].includes(publication.status);
  const action = publication.status === "review_pending"
    ? `<button class="publication-action confirm" data-publication-action="confirm">${publication.platform === "wechat_moments" ? "我已在微信发布" : "确认并发布"}</button>`
    : ["failed", "cancelled"].includes(publication.status)
      ? '<button class="publication-action" data-publication-action="retry">重试</button>'
      : active && publication.status !== "publishing"
        ? '<button class="publication-action subtle" data-publication-action="cancel">取消任务</button>' : "";
  elements.publicationPanel.innerHTML = `
    <div class="publication-heading"><strong>发布 Agent</strong><span class="publication-status ${publication.status}">${publicationStatusLabels[publication.status] || publication.status}</span></div>
    <div class="publication-visibility">可见范围：<strong>${visibilityLabels[publication.visibility] || "公开可见"}</strong></div>
    <p>${escapeHtml(publication.error_message || latestLog?.message || "任务状态已更新")}</p>
    ${publication.status === "review_pending" ? '<div class="review-callout">请先在平台窗口检查封面、分区、声明和可见范围，再回来确认。</div>' : ""}
    ${validationErrors.length ? `<div class="publication-errors">${validationErrors.map((item) => escapeHtml(item.message)).join("<br>")}</div>` : ""}
    ${publication.platform_url && ["submitted", "published"].includes(publication.status) ? `<a class="publication-link" href="${escapeHtml(publication.platform_url)}" target="_blank" rel="noreferrer">查看平台页面 ↗</a>` : ""}
    <div class="publication-actions">${action}<small>第 ${publication.attempt_count} 次尝试</small></div>`;
}

async function loadLatestPublication() {
  clearTimeout(state.publicationPollTimer);
  if (!state.adapterPost) return;
  const items = await api(`/api/publications?post_id=${encodeURIComponent(state.adapterPost.id)}&platform=${encodeURIComponent(state.adapterPlatform)}`);
  const publication = items[0] || null;
  renderPublication(publication);
  if (publication && ["pending", "validating", "queued", "awaiting_login", "preparing", "review_pending", "publishing"].includes(publication.status)) {
    state.publicationPollTimer = window.setTimeout(() => loadLatestPublication().catch((error) => toast(error.message, true)), 1500);
  }
}

async function openPlatformAdapter(post) {
  state.adapterPost = post;
  state.adapterPlatform = "douyin";
  state.browserSyncedContent = null;
  elements.publicationVisibility.value = "public";
  elements.platformModal.hidden = false;
  document.body.style.overflow = "hidden";
  await Promise.all([loadLlmSettings(), loadPlatformVersion("douyin")]);
}

function closePlatformAdapter() {
  clearTimeout(state.publicationPollTimer);
  elements.platformModal.hidden = true;
  document.body.style.overflow = "";
  state.adapterPost = null;
  state.platformVersion = null;
  state.publication = null;
}

function attemptClosePlatform() {
  if (state.platformDirty && !confirm("平台草稿有尚未保存的修改，确定关闭？")) return;
  closePlatformAdapter();
}

async function savePlatformDraft(showToast = true) {
  if (!state.adapterPost || !state.platformVersion) return null;
  const version = await api(`/api/posts/${state.adapterPost.id}/platform-versions/${state.adapterPlatform}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: elements.platformTitle.value,
      body: elements.platformBody.value,
      selected_asset_ids: selectedPlatformAssetIds(),
    }),
  });
  renderPlatformVersion(version);
  if (showToast) toast("平台发布草稿已保存");
  return version;
}

async function loadRoots() {
  const roots = await api("/api/library/roots");
  clearTimeout(state.scanPollTimer);
  if (!roots.length) {
    elements.rootList.innerHTML = '<div class="library-empty">尚未添加素材目录</div>';
    return;
  }
  const hasActiveScan = roots.some((root) => ["queued", "scanning"].includes(root.scan?.status));
  elements.rootList.innerHTML = roots.map((root) => `
    <article class="root-item">
      <div class="root-item-head">
        <div><strong>${escapeHtml(root.name)}</strong><small title="${escapeHtml(root.path)}">${escapeHtml(root.path)}</small></div>
        <div class="root-buttons">
          <button class="mini-button" type="button" data-scan-root="${root.id}" ${["queued", "scanning"].includes(root.scan?.status) ? "disabled" : ""}>${["queued", "scanning"].includes(root.scan?.status) ? "扫描中…" : "扫描"}</button>
          <button class="mini-button danger" type="button" data-delete-root="${root.id}">移除</button>
        </div>
      </div>
      <div class="root-stats"><span>${root.folder_count} 个目录</span><span>${root.asset_count} 张图片</span></div>
      ${["queued", "scanning"].includes(root.scan?.status) ? `<div class="scan-progress">正在后台索引：已检查 ${root.scan.progress?.files_seen || 0} 个文件，新增/更新 ${root.scan.progress?.indexed || 0} 个</div>` : ""}
      ${root.scan?.status === "failed" ? `<div class="scan-progress failed">扫描失败：${escapeHtml(root.scan.error || "未知错误")}，可以点击扫描重试</div>` : ""}
    </article>`).join("");
  elements.rootList.querySelectorAll("[data-scan-root]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true; button.textContent = "启动中…";
    try {
      await api(`/api/library/roots/${button.dataset.scanRoot}/scan`, { method: "POST" });
      toast("扫描已在后台开始，可以继续使用 Content Hub");
      await loadRoots();
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; button.textContent = "扫描"; }
  }));
  elements.rootList.querySelectorAll("[data-delete-root]").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("从索引中移除这个素材目录？原始文件不会被删除。")) return;
    try {
      await api(`/api/library/roots/${button.dataset.deleteRoot}`, { method: "DELETE" });
      await Promise.all([loadRoots(), searchLibrary()]);
    } catch (error) { toast(error.message, true); }
  }));
  if (hasActiveScan) {
    state.scanPollTimer = setTimeout(async () => {
      try { await Promise.all([loadRoots(), searchLibrary()]); }
      catch (error) { toast(error.message, true); }
    }, 1000);
  }
}

async function searchLibrary() {
  const params = new URLSearchParams();
  const coser = document.querySelector("#libraryCoserFilter").value.trim();
  const character = document.querySelector("#libraryCharacterFilter").value.trim();
  const date = document.querySelector("#libraryDateFilter").value;
  if (coser) params.set("coser_name", coser);
  if (character) params.set("character_name", character);
  if (date) params.set("shoot_date", date);
  const folders = await api(`/api/library/folders?${params}`);
  if (!folders.length) {
    elements.libraryResults.innerHTML = '<div class="library-empty">没有找到符合条件的素材目录。请检查目录命名或重新扫描。</div>';
    return;
  }
  elements.libraryResults.innerHTML = folders.map((folder) => `
    <article class="folder-result">
      <div>
        <h4 title="${escapeHtml(folder.path)}">${escapeHtml(folder.folder_name)}</h4>
        <div class="folder-meta">
          <span>Coser · ${escapeHtml(folder.coser_name || "未解析")}</span>
          <span>角色 · ${escapeHtml(folder.character_name || "未解析")}</span>
          <span>${escapeHtml(folder.shoot_date || "日期未解析")}</span>
        </div>
      </div>
      <div class="folder-count">${folder.asset_count}<small>张图片</small></div>
    </article>`).join("");
}

function renderAssets() {
  const storedAssets = state.editingPost?.assets || [];
  const matchesByAsset = new Map(state.assetMatches.map((match) => [match.downloaded_asset_id, match]));
  const storedMarkup = storedAssets.map((asset) => {
    const match = matchesByAsset.get(asset.id);
    let matchMarkup = `
      <div class="asset-match-info idle">
        <strong>${asset.media_type === "image" ? "尚未执行原图匹配" : "视频无需原图匹配"}</strong>
        <small>${asset.media_type === "image" ? "点击下方“开始匹配”查找高清素材" : "仅图片参与高清原图匹配"}</small>
      </div>
      <span class="asset-match-status idle" title="${asset.media_type === "image" ? "尚未匹配" : "不适用"}">—</span>`;

    if (match?.status === "matched") {
      matchMarkup = `
        <div class="asset-match-info matched">
          <strong title="${escapeHtml(match.original_filename || "")}">${escapeHtml(match.original_filename || "已匹配高清原图")}</strong>
          <small>${match.original_width ? `${match.original_width} × ${match.original_height}` : "尺寸未知"}${match.ssim_score != null ? ` · 相似度 ${(match.ssim_score * 100).toFixed(1)}%` : ""}</small>
          <span class="asset-match-path" title="${escapeHtml(match.original_path || "")}">${escapeHtml(match.original_path || "原图已复制到内容目录")}</span>
        </div>
        <span class="asset-match-status matched" title="匹配成功">✓</span>`;
    } else if (match?.status === "review") {
      matchMarkup = `
        <div class="asset-match-info review">
          <strong title="${escapeHtml(match.original_filename || "")}">${escapeHtml(match.original_filename || "发现可能的原图")}</strong>
          <small>${match.original_width ? `${match.original_width} × ${match.original_height}` : "尺寸未知"}${match.ssim_score != null ? ` · 相似度 ${(match.ssim_score * 100).toFixed(1)}%` : ""} <button class="asset-match-confirm" type="button" data-confirm-match="${match.id}">确认使用</button></small>
          <span class="asset-match-path" title="${escapeHtml(match.original_path || "")}">${escapeHtml(match.original_path || "等待人工确认")}</span>
        </div>
        <span class="asset-match-status review" title="需要确认">?</span>`;
    } else if (match?.status === "unmatched") {
      matchMarkup = `
        <div class="asset-match-info unmatched">
          <strong>未找到匹配原图</strong>
          <small>请检查角色标签或素材库索引</small>
          <span class="asset-match-path">未匹配到文件路径</span>
        </div>
        <span class="asset-match-status unmatched" title="匹配失败">×</span>`;
    }

    const previewUrl = match?.status === "matched" && match.original_url
      ? match.original_url : asset.url;
    const previewLabel = match?.status === "matched" && match.original_url
      ? `高清原图 · ${match.original_filename || asset.original_name}`
      : `下载图片 · ${asset.original_name}`;
    return `
      <div class="asset-item">
        ${asset.media_type === "image"
          ? `<button class="asset-thumb" type="button" data-preview-url="${escapeHtml(previewUrl)}" data-preview-label="${escapeHtml(previewLabel)}" title="点击查看${match?.status === "matched" && match.original_url ? "高清原图" : "下载图片"}"><img src="${asset.url}" alt=""></button>`
          : '<div class="asset-thumb">▶</div>'}
        <div class="asset-copy"><strong>${escapeHtml(asset.original_name)}</strong><small>${formatBytes(asset.file_size)}${asset.width ? ` · ${asset.width} × ${asset.height}` : ""}</small></div>
        ${matchMarkup}
        <button class="remove-asset" data-asset-id="${asset.id}" type="button" title="删除素材">⌫</button>
      </div>`;
  }).join("");
  const pendingMarkup = state.pendingFiles.map((file, index) => `
    <div class="asset-item pending-asset">
      <div class="asset-thumb">${file.type.startsWith("image/") ? "◇" : "▶"}</div>
      <div class="asset-copy"><strong>${escapeHtml(file.name)}</strong><small>${formatBytes(file.size)} · 等待上传</small></div>
      <button class="remove-asset" data-pending-index="${index}" type="button" title="移除">⌫</button>
    </div>`).join("");
  elements.assets.innerHTML = storedMarkup + pendingMarkup;
  elements.assets.querySelectorAll("[data-pending-index]").forEach((button) => button.addEventListener("click", () => {
    state.pendingFiles.splice(Number(button.dataset.pendingIndex), 1);
    renderAssets();
  }));
  elements.assets.querySelectorAll("[data-asset-id]").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("从内容中删除这个素材？此操作无法撤销。")) return;
    try {
      state.editingPost = await api(`/api/posts/${state.editingPost.id}/assets/${button.dataset.assetId}`, { method: "DELETE" });
      state.assetMatches = state.assetMatches.filter((match) => match.downloaded_asset_id !== button.dataset.assetId);
      renderAssets();
      toast("素材已删除");
    } catch (error) { toast(error.message, true); }
  }));
  elements.assets.querySelectorAll("[data-confirm-match]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await api(`/api/matches/${button.dataset.confirmMatch}/confirm`, { method: "POST" });
      await loadMatches(state.editingPost.id);
      toast("已确认并复制高清原图");
    } catch (error) { toast(error.message, true); }
  }));
  elements.assets.querySelectorAll("[data-preview-url]").forEach((button) => button.addEventListener("click", () => {
    openImageViewer(button.dataset.previewUrl, button.dataset.previewLabel);
  }));
}

function openImageViewer(url, caption) {
  elements.imageViewerImage.src = url;
  elements.imageViewerImage.alt = caption || "图片预览";
  elements.imageViewerCaption.textContent = caption || "";
  elements.imageViewer.hidden = false;
}

function closeImageViewer() {
  elements.imageViewer.hidden = true;
  elements.imageViewerImage.removeAttribute("src");
}

function renderMatches(matches) {
  state.assetMatches = matches;
  elements.manualMatchButton.hidden = !matches.some((match) => match.status === "unmatched");
  renderAssets();
}

async function loadMatches(postId) {
  const matches = await api(`/api/posts/${postId}/matches`);
  if (state.editingPost?.id === postId) renderMatches(matches);
}

function addFiles(fileList) {
  const incoming = [...fileList].filter((file) => file.type.startsWith("image/") || file.type.startsWith("video/"));
  state.pendingFiles.push(...incoming);
  renderAssets();
  elements.fileInput.value = "";
}

async function uploadPending(postId) {
  if (!state.pendingFiles.length) return null;
  const formData = new FormData();
  state.pendingFiles.forEach((file) => formData.append("files", file));
  return api(`/api/posts/${postId}/assets`, { method: "POST", body: formData });
}

async function saveEditorContent({ closeAfter = true, showToast = true } = {}) {
  const payload = {
    title: elements.title.value.trim(),
    body: elements.body.value,
    tags: elements.tags.value.split(/[,，\n]/).map((tag) => tag.trim()).filter(Boolean),
    status: elements.status.value,
    source_platform: elements.source.value,
    source_url: elements.sourceUrl.value.trim() || null,
    content_type: state.editingPost?.content_type || "gallery",
  };
  const wasEditing = Boolean(state.editingPost);
  elements.save.disabled = true;
  elements.save.textContent = state.pendingFiles.length ? "正在保存与上传…" : "正在保存…";
  try {
    let post = state.editingPost
      ? await api(`/api/posts/${state.editingPost.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      : await api("/api/posts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    post = await uploadPending(post.id) || post;
    state.editingPost = post;
    state.pendingFiles = [];
    if (closeAfter) closeEditor();
    await Promise.all([loadPosts(), loadDashboard()]);
    if (showToast) toast(wasEditing ? "内容已更新" : "内容已创建");
    return post;
  } finally {
    elements.save.disabled = false;
    elements.save.textContent = "保存内容";
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await saveEditorContent();
  } catch (error) {
    toast(error.message, true);
  }
});

elements.deletePost.addEventListener("click", async () => {
  if (!state.editingPost || !confirm(`确定删除“${state.editingPost.title || "未命名内容"}”及其全部素材？`)) return;
  try {
    await api(`/api/posts/${state.editingPost.id}`, { method: "DELETE" });
    closeEditor();
    await Promise.all([loadPosts(), loadDashboard()]);
    toast("内容已删除");
  } catch (error) { toast(error.message, true); }
});

elements.importForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.startImport.disabled = true;
  elements.startImport.textContent = "正在逐条解析并下载…";
  try {
    const job = await api("/api/imports/batch-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        platform: elements.importPlatform.value,
        text: elements.importUrl.value.trim(),
        confirm_rights: elements.confirmRights.checked,
      }),
    });
    state.importJob = job;
    renderImportJob(job);
    await pollImportJob(job.id);
  } catch (error) {
    toast(error.message, true);
    elements.startImport.disabled = false;
    elements.startImport.textContent = "批量解析并导入";
  }
});

function renderImportJob(job) {
  state.importJob = job;
  elements.batchImportProgress.hidden = false;
  elements.batchImportProgressBar.style.width = `${job.progress || 0}%`;
  const track = elements.batchImportProgress.querySelector(".progress-track");
  track.setAttribute("aria-valuenow", String(Math.round(job.progress || 0)));
  const current = job.current_index || 0;
  const name = job.current_name || "等待开始";
  elements.batchImportProgressText.textContent = `${name} 第[${current}]/[${job.total}]条，图片已下载 第[${job.image_downloaded || 0}]/[${job.image_total || 0}]张。`;
  elements.batchImportResults.hidden = !(job.results?.length || job.status === "failed");
  elements.batchImportResults.innerHTML = `
    <div class="batch-result-summary">成功 ${job.imported || 0} · 跳过 ${job.skipped || 0} · 失败 ${job.failed || 0}</div>
    ${(job.results || []).map((item) => `<div class="batch-result-item ${item.status}">
      <span>${item.status === "imported" ? "✓" : item.status === "skipped" ? "—" : "×"}</span>
      <div><strong>${escapeHtml(item.post?.title || item.url)}</strong><small>${escapeHtml(item.error || `${item.post?.assets?.length || 0} 个素材`)}</small></div>
    </div>`).join("")}
    ${job.error ? `<div class="batch-result-item failed"><span>×</span><div><strong>批次中断</strong><small>${escapeHtml(job.error)}</small></div></div>` : ""}`;
}

async function pollImportJob(jobId) {
  clearTimeout(state.importPollTimer);
  const job = await api(`/api/imports/batch-jobs/${jobId}`);
  renderImportJob(job);
  if (["completed", "failed"].includes(job.status)) {
    elements.startImport.disabled = false;
    elements.startImport.textContent = "批量解析并导入";
    await Promise.all([loadPosts(), loadDashboard()]);
    toast(job.status === "completed"
      ? `批量导入完成：成功 ${job.imported}，失败 ${job.failed}`
      : `批量导入中断：${job.error || "未知错误"}`, job.status === "failed");
    return;
  }
  state.importPollTimer = window.setTimeout(
    () => pollImportJob(jobId).catch((error) => toast(error.message, true)), 500,
  );
}

function renderImportPlatform() {
  const douyin = elements.importPlatform.value === "douyin";
  elements.importPlatformMark.textContent = douyin ? "抖音" : "小红书";
  elements.importPlatformMark.classList.toggle("douyin", douyin);
  elements.importPlatformName.textContent = douyin ? "抖音公开作品" : "小红书公开作品";
  elements.importUrl.placeholder = douyin
    ? "粘贴多个抖音作品链接，使用空格或换行分隔…"
    : "粘贴多个小红书作品链接，使用空格或换行分隔…";
}

elements.importPlatform.addEventListener("change", renderImportPlatform);

elements.aiConfigForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.querySelector("#saveAiConfigButton");
  button.disabled = true; button.textContent = "保存中…";
  try {
    const payload = { model: elements.doubaoModel.value.trim() };
    if (elements.doubaoApiKey.value.trim()) payload.api_key = elements.doubaoApiKey.value.trim();
    state.llmSettings = await api("/api/settings/llm", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    await loadLlmSettings();
    toast("豆包配置已保存");
    closeAiConfig();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "保存配置"; }
});

document.querySelector("#clearApiKeyButton").addEventListener("click", async () => {
  if (!confirm("确定清除当前设备保存的豆包 API Key？")) return;
  try {
    state.llmSettings = await api("/api/settings/llm", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ clear_api_key: true }),
    });
    await loadLlmSettings();
    toast("API Key 已清除");
  } catch (error) { toast(error.message, true); }
});

elements.openPlatformButton.addEventListener("click", async () => {
  if (!state.editingPost || !elements.form.reportValidity()) return;
  elements.openPlatformButton.disabled = true;
  try {
    const post = await saveEditorContent({ closeAfter: false, showToast: false });
    closeEditor();
    await openPlatformAdapter(post);
  } catch (error) { toast(error.message, true); }
  finally { elements.openPlatformButton.disabled = false; }
});

elements.targetPlatform.addEventListener("change", async () => {
  const nextPlatform = elements.targetPlatform.value;
  elements.targetPlatform.disabled = true;
  try {
    if (state.platformDirty) await savePlatformDraft(false);
    await loadPlatformVersion(nextPlatform);
  } catch (error) { toast(error.message, true); }
  finally { elements.targetPlatform.disabled = false; }
});

[elements.platformTitle, elements.platformBody, elements.generationPrompt].forEach((input) => {
  input.addEventListener("input", () => { state.platformDirty = true; });
});

elements.generateCopy.addEventListener("click", async () => {
  if (!state.adapterPost || !state.llmSettings?.has_api_key) return;
  elements.generateCopy.disabled = true;
  elements.generateCopy.textContent = "✦ 豆包生成中…";
  try {
    const version = await api(`/api/posts/${state.adapterPost.id}/platform-versions/${state.adapterPlatform}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        selected_asset_ids: selectedPlatformAssetIds(),
        custom_prompt: elements.generationPrompt.value.trim() || null,
      }),
    });
    renderPlatformVersion(version);
    toast("已生成新的平台标题和正文");
  } catch (error) { toast(error.message, true); }
  finally {
    elements.generateCopy.disabled = !state.llmSettings?.has_api_key;
    elements.generateCopy.textContent = state.platformVersion?.generation_count > 0 ? "✦ 再次生成" : "✦ 一键生成";
  }
});

elements.savePlatform.addEventListener("click", async () => {
  elements.savePlatform.disabled = true; elements.savePlatform.textContent = "保存中…";
  try { await savePlatformDraft(); }
  catch (error) { toast(error.message, true); }
  finally { elements.savePlatform.disabled = false; elements.savePlatform.textContent = "保存发布草稿"; }
});

elements.preparePublish.addEventListener("click", async () => {
  elements.preparePublish.disabled = true;
  elements.preparePublish.textContent = "正在启动…";
  try {
    await savePlatformDraft(false);
    const publication = await api("/api/publications", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        post_id: state.adapterPost.id,
        platform: state.adapterPlatform,
        visibility: elements.publicationVisibility.value,
      }),
    });
    renderPublication(publication);
    toast(`发布 Agent 已打开${platformLabels[state.adapterPlatform]}窗口`);
    await loadLatestPublication();
  } catch (error) { toast(error.message, true); }
  finally { elements.preparePublish.disabled = false; elements.preparePublish.textContent = "准备发布"; }
});

elements.publicationPanel.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-publication-action]");
  if (!button || !state.publication) return;
  const action = button.dataset.publicationAction;
  const visibilityLabels = { public: "公开可见", friends: "仅互关好友可见", private: "仅自己可见" };
  if (action === "confirm" && !confirm(`确认将当前内容发布到${platformLabels[state.adapterPlatform]}，可见范围为“${visibilityLabels[state.publication.visibility]}”？`)) return;
  button.disabled = true;
  try {
    await api(`/api/publications/${state.publication.id}/${action}`, { method: "POST" });
    toast(action === "confirm" ? "已确认，Agent 正在提交作品" : action === "retry" ? "已重新打开平台窗口" : "正在取消任务");
    await loadLatestPublication();
  } catch (error) { toast(error.message, true); button.disabled = false; }
});

elements.pickRootButton.addEventListener("click", async () => {
  const button = elements.pickRootButton;
  const originalContent = button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span class="folder-picker-icon">…</span><span><strong>等待选择目录</strong><small>请在 Windows 窗口中选择素材文件夹</small></span>';
  try {
    const selected = await api("/api/system/pick-folder", { method: "POST" });
    if (selected.cancelled || !selected.path) return;
    button.innerHTML = '<span class="folder-picker-icon">✓</span><span><strong>正在登记目录</strong><small>扫描将在后台继续</small></span>';
    const root = await api("/api/library/roots", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: selected.path }),
    });
    await api(`/api/library/roots/${root.id}/scan`, { method: "POST" });
    toast(`已添加“${root.name}”，正在后台建立索引`);
    await loadRoots();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.innerHTML = originalContent; }
});

elements.librarySearchForm.addEventListener("submit", (event) => {
  event.preventDefault(); searchLibrary().catch((error) => toast(error.message, true));
});

elements.matchButton.addEventListener("click", async () => {
  if (!state.editingPost) return;
  elements.matchButton.disabled = true; elements.matchButton.textContent = "匹配中…";
  try {
    const result = await api(`/api/posts/${state.editingPost.id}/match-originals`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    renderMatches(result.matches);
    toast(result.folders.length
      ? `已检索 ${result.folders.length} 个角色目录、${result.searched_assets} 张原图`
      : "没有找到标签对应的角色目录，请先检查素材库索引");
  } catch (error) { toast(error.message, true); }
  finally { elements.matchButton.disabled = false; elements.matchButton.textContent = "开始匹配"; }
});

elements.manualMatchButton.addEventListener("click", async () => {
  if (!state.editingPost) return;
  elements.manualMatchButton.disabled = true;
  elements.manualMatchButton.textContent = "选择文件夹…";
  try {
    const selected = await api("/api/system/pick-original-folder", { method: "POST" });
    if (selected.cancelled || !selected.path) return;
    elements.manualMatchButton.textContent = "扫描并匹配中…";
    const result = await api(`/api/posts/${state.editingPost.id}/match-originals/manual`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: selected.path }),
    });
    renderMatches(result.matches);
    toast(`已在指定目录检索 ${result.searched_assets} 张原图`);
  } catch (error) { toast(error.message, true); }
  finally {
    elements.manualMatchButton.disabled = false;
    elements.manualMatchButton.textContent = "指定原图文件夹";
  }
});

elements.startBatchPublish.addEventListener("click", async () => {
  const platforms = [...document.querySelectorAll(".queue-platforms input:checked")]
    .map((input) => input.value);
  if (!elements.queuePostSelect.value) return toast("请先选择包含素材的本地内容", true);
  if (!platforms.length) return toast("请至少选择一个目标平台", true);
  elements.startBatchPublish.disabled = true;
  elements.startBatchPublish.textContent = "正在创建发布任务…";
  try {
    const result = await api("/api/publications/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        post_id: elements.queuePostSelect.value,
        platforms,
        visibility: elements.queueVisibility.value,
      }),
    });
    state.queueFocusIds = result.created.map((item) => item.id);
    if (result.created.length) {
      toast(`已创建 ${result.created.length} 个平台发布任务${result.skipped.length ? `，跳过 ${result.skipped.length} 个进行中任务` : ""}`);
    } else {
      toast("所选平台已有进行中的发布任务", true);
    }
    await loadPublishQueue();
  } catch (error) { toast(error.message, true); }
  finally {
    elements.startBatchPublish.disabled = false;
    elements.startBatchPublish.textContent = "加入多平台发布队列";
  }
});

elements.queueList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-queue-action]");
  const item = event.target.closest("[data-publication-id]");
  if (!button || !item) return;
  const publication = state.queuePublications.find((value) => value.id === item.dataset.publicationId);
  if (!publication) return;
  const action = button.dataset.queueAction;
  if (action === "confirm" && !confirm(`确认发布到${platformLabels[publication.platform] || publication.platform}？请先检查已打开的平台窗口。`)) return;
  button.disabled = true;
  try {
    await api(`/api/publications/${publication.id}/${action}`, { method: "POST" });
    toast(action === "confirm" ? "已确认，发布 Agent 正在提交" : action === "retry" ? "已重新启动发布任务" : "正在取消任务");
    await loadPublishQueue();
  } catch (error) { toast(error.message, true); button.disabled = false; }
});

document.querySelector("#newPostButton").addEventListener("click", () => openEditor());
document.querySelector("#platformManagerButton").addEventListener("click", () => openPlatformManager().catch((error) => toast(error.message, true)));
document.querySelector("#closePlatformManagerButton").addEventListener("click", closePlatformManager);
elements.platformManagerModal.addEventListener("click", (event) => { if (event.target === elements.platformManagerModal) closePlatformManager(); });
elements.platformManagerFilter.addEventListener("change", renderPublishedWorks);
document.querySelector("#publishQueueButton").addEventListener("click", () => openPublishQueue().catch((error) => toast(error.message, true)));
document.querySelector("#closePublishQueueButton").addEventListener("click", closePublishQueue);
elements.publishQueueModal.addEventListener("click", (event) => { if (event.target === elements.publishQueueModal) closePublishQueue(); });
document.querySelector("#storageSettingsButton").addEventListener("click", () => openStorageSettings().catch((error) => toast(error.message, true)));
document.querySelector("#closeStorageButton").addEventListener("click", closeStorageSettings);
document.querySelector("#cancelStorageButton").addEventListener("click", closeStorageSettings);
elements.storageModal.addEventListener("click", (event) => { if (event.target === elements.storageModal) closeStorageSettings(); });
elements.pickStorage.addEventListener("click", async () => {
  elements.pickStorage.disabled = true;
  try {
    const selected = await api("/api/system/pick-storage-folder", { method: "POST" });
    if (!selected.cancelled && selected.path) elements.storagePath.value = selected.path;
  } catch (error) { toast(error.message, true); }
  finally { elements.pickStorage.disabled = false; }
});
elements.saveStorage.addEventListener("click", async () => {
  if (!elements.storagePath.value.trim()) return;
  elements.saveStorage.disabled = true;
  elements.saveStorage.textContent = "正在复制并校验…";
  try {
    const result = await api("/api/settings/storage", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: elements.storagePath.value.trim() }),
    });
    toast(`存储目录已切换，复制 ${result.copied_files} 个文件`);
    closeStorageSettings();
  } catch (error) { toast(error.message, true); }
  finally { elements.saveStorage.disabled = false; elements.saveStorage.textContent = "迁移并保存"; }
});
document.querySelector("#accountManagerButton").addEventListener("click", () => openAccountManager().catch((error) => toast(error.message, true)));
document.querySelector("#closeAccountButton").addEventListener("click", closeAccountManager);
document.querySelector("#doneAccountButton").addEventListener("click", closeAccountManager);
elements.refreshAccounts.addEventListener("click", () => checkAccounts().catch((error) => toast(error.message, true)));
elements.accountModal.addEventListener("click", (event) => { if (event.target === elements.accountModal) closeAccountManager(); });
elements.accountGrid.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-login-platform]");
  if (!button) return;
  button.disabled = true;
  try {
    await api(`/api/accounts/${button.dataset.loginPlatform}/login`, { method: "POST" });
    toast(`已打开${platformLabels[button.dataset.loginPlatform]}登录网页`);
    await loadAccounts();
  } catch (error) { toast(error.message, true); button.disabled = false; }
});
document.querySelector("#aiConfigButton").addEventListener("click", () => openAiConfig().catch((error) => toast(error.message, true)));
document.querySelector("#closeAiConfigButton").addEventListener("click", closeAiConfig);
document.querySelector("#cancelAiConfigButton").addEventListener("click", closeAiConfig);
elements.aiConfigModal.addEventListener("click", (event) => { if (event.target === elements.aiConfigModal) closeAiConfig(); });
document.querySelector("#closePlatformButton").addEventListener("click", attemptClosePlatform);
document.querySelector("#cancelPlatformButton").addEventListener("click", attemptClosePlatform);
elements.platformModal.addEventListener("click", (event) => { if (event.target === elements.platformModal) attemptClosePlatform(); });
elements.llmReadiness.addEventListener("click", () => {
  if (!state.llmSettings?.has_api_key) openAiConfig().catch((error) => toast(error.message, true));
});
document.querySelector("#libraryButton").addEventListener("click", () => openLibrary().catch((error) => toast(error.message, true)));
document.querySelector("#closeLibraryButton").addEventListener("click", closeLibrary);
elements.libraryModal.addEventListener("click", (event) => { if (event.target === elements.libraryModal) closeLibrary(); });
document.querySelector("#importLinkButton").addEventListener("click", openImport);
document.querySelector("#closeImportButton").addEventListener("click", closeImport);
document.querySelector("#cancelImportButton").addEventListener("click", closeImport);
elements.importModal.addEventListener("click", (event) => { if (event.target === elements.importModal) closeImport(); });
document.querySelector("#closeModalButton").addEventListener("click", closeEditor);
document.querySelector("#cancelButton").addEventListener("click", closeEditor);
elements.modal.addEventListener("click", (event) => { if (event.target === elements.modal) closeEditor(); });
document.querySelector("#closeImageViewerButton").addEventListener("click", closeImageViewer);
elements.imageViewer.addEventListener("click", (event) => { if (event.target === elements.imageViewer) closeImageViewer(); });
elements.body.addEventListener("input", () => { elements.bodyCount.textContent = elements.body.value.length; });
elements.fileInput.addEventListener("change", () => addFiles(elements.fileInput.files));
elements.dropzone.addEventListener("dragover", (event) => { event.preventDefault(); elements.dropzone.classList.add("dragging"); });
elements.dropzone.addEventListener("dragleave", () => elements.dropzone.classList.remove("dragging"));
elements.dropzone.addEventListener("drop", (event) => {
  event.preventDefault(); elements.dropzone.classList.remove("dragging"); addFiles(event.dataTransfer.files);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!elements.imageViewer.hidden) closeImageViewer();
  else if (!elements.publishQueueModal.hidden) closePublishQueue();
  else if (!elements.platformManagerModal.hidden) closePlatformManager();
  else if (!elements.storageModal.hidden) closeStorageSettings();
  else if (!elements.accountModal.hidden) closeAccountManager();
  else if (!elements.aiConfigModal.hidden) closeAiConfig();
  else if (!elements.platformModal.hidden) attemptClosePlatform();
  else if (!elements.libraryModal.hidden) closeLibrary();
  else if (!elements.importModal.hidden) closeImport();
  else if (!elements.modal.hidden) closeEditor();
});

let searchTimer;
elements.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadPosts().catch((error) => toast(error.message, true)), 250);
});
[elements.statusFilter, elements.typeFilter].forEach((element) => element.addEventListener("change", () => loadPosts().catch((error) => toast(error.message, true))));
document.querySelectorAll("[data-view]").forEach((button) => {
  button.classList.toggle("active", button.dataset.view === state.view);
  button.addEventListener("click", () => {
    state.view = button.dataset.view;
    localStorage.setItem("content-hub-view", state.view);
    document.querySelectorAll("[data-view]").forEach((item) => item.classList.toggle("active", item === button));
    renderPosts();
  });
});

Promise.all([loadDashboard(), loadPosts()]).catch((error) => {
  elements.grid.innerHTML = `<div class="empty-state"><h3>内容载入失败</h3><p>${escapeHtml(error.message)}</p></div>`;
  toast(error.message, true);
});
