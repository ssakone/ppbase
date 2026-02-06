/**
 * PPBase Admin - Main Application Controller
 *
 * Handles login/logout, navigation, sidebar, toasts, and modals.
 * Delegates rendering to CollectionsUI and RecordsUI.
 */
const App = {
  // ── State ───────────────────────────────────────────────────

  currentView: null,         // 'collections' | 'collection-detail' | 'records' | 'settings'
  currentCollection: null,   // currently selected collection object
  collections: [],           // cached list of all collections

  // ── DOM References ──────────────────────────────────────────

  els: {},

  cacheElements() {
    this.els = {
      loginScreen:    document.getElementById('login-screen'),
      dashboard:      document.getElementById('dashboard'),
      loginForm:      document.getElementById('login-form'),
      loginEmail:     document.getElementById('login-email'),
      loginPassword:  document.getElementById('login-password'),
      loginError:     document.getElementById('login-error'),
      loginBtn:       document.getElementById('login-btn'),
      collectionsNav: document.getElementById('collections-nav'),
      contentHeader:  document.getElementById('content-header'),
      contentBody:    document.getElementById('content-body'),
      contentActions: document.getElementById('content-actions'),
      breadcrumb:     document.getElementById('breadcrumb'),
      modalOverlay:   document.getElementById('modal-overlay'),
      modalTitle:     document.getElementById('modal-title'),
      modalBody:      document.getElementById('modal-body'),
      modalFooter:    document.getElementById('modal-footer'),
      modalClose:     document.getElementById('modal-close'),
      toastContainer: document.getElementById('toast-container'),
      navSettings:    document.getElementById('nav-settings'),
      navLogout:      document.getElementById('nav-logout'),
    };
  },

  // ── Initialization ──────────────────────────────────────────

  async init() {
    this.cacheElements();
    this.bindEvents();

    // Check for existing session
    if (PBClient.getToken()) {
      try {
        await PBClient.listCollections('perPage=1');
        this.showDashboard();
      } catch (e) {
        PBClient.clearToken();
        this.showLogin();
      }
    } else {
      this.showLogin();
    }
  },

  // ── Event Binding ───────────────────────────────────────────

  bindEvents() {
    // Login form
    this.els.loginForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const email = this.els.loginEmail.value.trim();
      const password = this.els.loginPassword.value;
      if (email && password) {
        this.handleLogin(email, password);
      }
    });

    // Sidebar footer
    this.els.navSettings.addEventListener('click', (e) => {
      e.preventDefault();
      this.navigate('settings');
    });

    this.els.navLogout.addEventListener('click', (e) => {
      e.preventDefault();
      this.handleLogout();
    });

    // Modal close
    this.els.modalClose.addEventListener('click', () => this.closeModal());
    this.els.modalOverlay.addEventListener('click', (e) => {
      if (e.target === this.els.modalOverlay) this.closeModal();
    });

    // Keyboard: Escape closes modal
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !this.els.modalOverlay.classList.contains('hidden')) {
        this.closeModal();
      }
    });
  },

  // ── Auth ────────────────────────────────────────────────────

  async handleLogin(email, password) {
    const btn = this.els.loginBtn;
    const originalText = btn.textContent;

    // Show loading state
    btn.disabled = true;
    btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Signing in...';
    this.els.loginError.classList.add('hidden');

    try {
      const result = await PBClient.login(email, password);
      PBClient.setToken(result.token);
      this.showDashboard();
    } catch (err) {
      const message = (err && err.message) || 'Invalid email or password.';
      this.els.loginError.textContent = message;
      this.els.loginError.classList.remove('hidden');
      this.els.loginPassword.value = '';
      this.els.loginPassword.focus();
    } finally {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  },

  handleLogout() {
    PBClient.clearToken();
    this.collections = [];
    this.currentCollection = null;
    this.currentView = null;
    this.showLogin();
  },

  // ── View Switching ──────────────────────────────────────────

  showLogin() {
    this.els.loginScreen.classList.remove('hidden');
    this.els.dashboard.classList.add('hidden');
    this.els.loginEmail.value = '';
    this.els.loginPassword.value = '';
    this.els.loginError.classList.add('hidden');
    this.els.loginEmail.focus();
  },

  async showDashboard() {
    this.els.loginScreen.classList.add('hidden');
    this.els.dashboard.classList.remove('hidden');
    await this.loadSidebarCollections();
    this.navigate('collections');
  },

  // ── Sidebar ─────────────────────────────────────────────────

  async loadSidebarCollections() {
    const nav = this.els.collectionsNav;

    // Show skeleton loading
    nav.innerHTML = `
      <div class="sidebar-skeleton">
        <div class="skeleton-item"></div>
        <div class="skeleton-item"></div>
        <div class="skeleton-item"></div>
      </div>
    `;

    try {
      const result = await PBClient.listCollections('perPage=200');
      this.collections = result.items || result || [];
      this.renderSidebarCollections();
    } catch (err) {
      nav.innerHTML = `
        <div style="padding: 0.75rem; font-size: 0.8125rem; color: #dc2626;">
          Failed to load collections
        </div>
      `;
    }
  },

  renderSidebarCollections() {
    const nav = this.els.collectionsNav;
    nav.innerHTML = '';

    if (this.collections.length === 0) {
      nav.innerHTML = `
        <div style="padding: 0.75rem; font-size: 0.8125rem; color: #9ca3af;">
          No collections yet
        </div>
      `;
      return;
    }

    this.collections.forEach((col) => {
      const item = document.createElement('a');
      item.href = '#';
      item.className = 'nav-item';
      if (this.currentCollection && this.currentCollection.id === col.id) {
        item.classList.add('active');
      }

      item.innerHTML = `
        <span class="nav-item-icon"></span>
        <span class="truncate">${this.escapeHtml(col.name)}</span>
      `;

      item.addEventListener('click', (e) => {
        e.preventDefault();
        this.currentCollection = col;
        this.renderSidebarCollections();
        this.navigate('records', col);
      });

      nav.appendChild(item);
    });
  },

  // ── Navigation / Routing ────────────────────────────────────

  navigate(view, data) {
    this.currentView = view;

    switch (view) {
      case 'collections':
        this.currentCollection = null;
        this.renderSidebarCollections();
        this.setBreadcrumb([{ label: 'Collections', active: true }]);
        this.setActions(`
          <button class="btn btn-primary" id="btn-new-collection">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M7 1v12M1 7h12"/></svg>
            New collection
          </button>
        `);
        this.bindActionEvent('btn-new-collection', () => CollectionsUI.showCreateModal());
        CollectionsUI.renderList(this.collections);
        break;

      case 'records': {
        if (data) this.currentCollection = data;
        const isView = this.currentCollection.type === 'view';
        this.setBreadcrumb([
          { label: 'Collections', onClick: () => this.navigate('collections') },
          { label: this.currentCollection.name, active: true },
        ]);
        let recordActions = `
          <button class="btn btn-secondary btn-sm" id="btn-collection-settings" style="margin-right: 0.375rem;">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="8" cy="8" r="2.5"/>
              <path d="M13.5 8a5.5 5.5 0 01-.3 1.2l1.1.9a.3.3 0 01.1.4l-1.1 1.9a.3.3 0 01-.4.1l-1.3-.5a5 5 0 01-1 .6l-.2 1.4a.3.3 0 01-.3.2H8a.3.3 0 01-.3-.2l-.2-1.4a5 5 0 01-1-.6l-1.3.5a.3.3 0 01-.4-.1L3.7 10.5a.3.3 0 01.1-.4l1.1-.9A5.5 5.5 0 014.5 8c0-.4 0-.8.1-1.2l-1.1-.9a.3.3 0 01-.1-.4l1.1-1.9a.3.3 0 01.4-.1l1.3.5a5 5 0 011-.6l.2-1.4a.3.3 0 01.3-.2H10a.3.3 0 01.3.2l.2 1.4a5 5 0 011 .6l1.3-.5a.3.3 0 01.4.1l1.1 1.9a.3.3 0 01-.1.4l-1.1.9c.1.4.2.8.2 1.2z"/>
            </svg>
            Schema
          </button>
        `;
        if (!isView) {
          recordActions += `
            <button class="btn btn-primary" id="btn-new-record">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M7 1v12M1 7h12"/></svg>
              New record
            </button>
          `;
        }
        this.setActions(recordActions);
        this.bindActionEvent('btn-collection-settings', () => {
          CollectionsUI.showDetailModal(this.currentCollection);
        });
        if (!isView) {
          this.bindActionEvent('btn-new-record', () => {
            RecordsUI.showCreateModal(this.currentCollection);
          });
        }
        RecordsUI.loadAndRender(this.currentCollection);
        break;
      }

      case 'settings':
        this.currentCollection = null;
        this.renderSidebarCollections();
        this.setBreadcrumb([{ label: 'Settings', active: true }]);
        this.setActions('');
        this.renderSettings();
        break;

      default:
        this.navigate('collections');
    }
  },

  // ── Breadcrumb ──────────────────────────────────────────────

  setBreadcrumb(items) {
    const bc = this.els.breadcrumb;
    bc.innerHTML = '';

    items.forEach((item, i) => {
      if (i > 0) {
        const sep = document.createElement('span');
        sep.className = 'breadcrumb-separator';
        bc.appendChild(sep);
      }

      const span = document.createElement('span');
      span.className = 'breadcrumb-item' + (item.active ? ' active' : '');
      span.textContent = item.label;

      if (item.onClick) {
        span.style.cursor = 'pointer';
        span.addEventListener('click', item.onClick);
      }

      bc.appendChild(span);
    });
  },

  // ── Content Actions ─────────────────────────────────────────

  setActions(html) {
    this.els.contentActions.innerHTML = html;
  },

  bindActionEvent(id, handler) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', handler);
  },

  // ── Toast Notifications ─────────────────────────────────────

  showToast(message, type = 'success') {
    const container = this.els.toastContainer;
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    const iconSvg = type === 'success'
      ? '<svg class="toast-icon" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9l3.5 3.5L14 5"/></svg>'
      : '<svg class="toast-icon" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="9" r="7"/><path d="M9 6v4M9 12.5v.5"/></svg>';

    toast.innerHTML = iconSvg + '<span>' + this.escapeHtml(message) + '</span>';
    container.appendChild(toast);

    // Auto-remove after animation completes
    setTimeout(() => {
      if (toast.parentNode) toast.remove();
    }, 4200);
  },

  // ── Modal System ────────────────────────────────────────────

  showModal(title, bodyHtml, footerHtml, options = {}) {
    this.els.modalTitle.textContent = title;
    this.els.modalBody.innerHTML = bodyHtml;
    this.els.modalFooter.innerHTML = footerHtml || '';

    const modal = this.els.modalOverlay.querySelector('.modal');
    if (options.wide) {
      modal.classList.add('modal-wide');
    } else {
      modal.classList.remove('modal-wide');
    }

    this.els.modalOverlay.classList.remove('hidden');

    // Focus first input in modal
    requestAnimationFrame(() => {
      const firstInput = this.els.modalBody.querySelector('input, select, textarea');
      if (firstInput) firstInput.focus();
    });
  },

  closeModal() {
    this.els.modalOverlay.classList.add('hidden');
    this.els.modalBody.innerHTML = '';
    this.els.modalFooter.innerHTML = '';
  },

  // ── Settings View ───────────────────────────────────────────

  async renderSettings() {
    const body = this.els.contentBody;
    body.innerHTML = '<div class="content-loading"><div class="spinner"></div></div>';

    try {
      const settings = await PBClient.getSettings();
      body.innerHTML = `
        <div class="settings-section">
          <h3>Application Settings</h3>
          <p>Configure your PPBase instance.</p>
          <div class="card">
            <div class="card-body">
              <form id="settings-form" class="flex-col gap-4">
                <div class="form-group">
                  <label class="form-label">Application Name</label>
                  <input class="form-input" type="text" name="meta.appName"
                    value="${this.escapeHtml((settings.meta && settings.meta.appName) || '')}"
                    placeholder="PPBase">
                </div>
                <div class="form-group">
                  <label class="form-label">Application URL</label>
                  <input class="form-input" type="url" name="meta.appUrl"
                    value="${this.escapeHtml((settings.meta && settings.meta.appUrl) || '')}"
                    placeholder="https://example.com">
                </div>
                <div>
                  <button type="submit" class="btn btn-primary">Save settings</button>
                </div>
              </form>
            </div>
          </div>
        </div>
      `;

      document.getElementById('settings-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const form = e.target;
        const data = {
          meta: {
            appName: form.querySelector('[name="meta.appName"]').value,
            appUrl: form.querySelector('[name="meta.appUrl"]').value,
          },
        };
        try {
          await PBClient.updateSettings(data);
          this.showToast('Settings updated successfully.');
        } catch (err) {
          this.showToast('Failed to update settings.', 'error');
        }
      });
    } catch (err) {
      body.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
              <circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>
            </svg>
          </div>
          <h3>Unable to load settings</h3>
          <p>There was an error loading the application settings. Please try again.</p>
        </div>
      `;
    }
  },

  // ── Helpers ─────────────────────────────────────────────────

  escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  },

  formatDate(dateStr) {
    if (!dateStr) return '-';
    try {
      const d = new Date(dateStr);
      return d.toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      return dateStr;
    }
  },
};

// ── Bootstrap ─────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => App.init());
