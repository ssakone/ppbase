/**
 * PPBase Admin - Main Application Controller
 *
 * Handles login/logout, navigation, sidebar, toasts, and modals.
 * Delegates rendering to CollectionsUI and RecordsUI.
 */
const App = {
  // ── State ───────────────────────────────────────────────────

  currentView: null,         // 'collections' | 'collection-detail' | 'records' | 'migrations' | 'settings'
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
      collectionsSearch: document.getElementById('collections-search'),
      collectionsPanel: document.querySelector('.sidebar-collections-panel'),
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
      navIconCollections: document.getElementById('nav-icon-collections'),
      navIconMigrations:  document.getElementById('nav-icon-migrations'),
      navIconSettings:    document.getElementById('nav-icon-settings'),
      navLogout:      document.getElementById('nav-logout'),
      btnSidebarNewCollection: document.getElementById('btn-sidebar-new-collection'),
      drawerOverlay:  document.getElementById('drawer-overlay'),
      drawer:         document.getElementById('drawer'),
      drawerTitle:    document.getElementById('drawer-title'),
      drawerBody:     document.getElementById('drawer-body'),
      drawerFooter:   document.getElementById('drawer-footer'),
      drawerClose:    document.getElementById('drawer-close'),
      drawerHeaderActions: document.getElementById('drawer-header-actions'),
      selectionBar:   document.getElementById('selection-bar'),
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

    // Icon strip navigation
    this.els.navIconCollections.addEventListener('click', (e) => {
      e.preventDefault();
      this.setActiveIcon('collections');
      this.navigate('collections');
    });

    this.els.navIconMigrations.addEventListener('click', (e) => {
      e.preventDefault();
      this.setActiveIcon('migrations');
      this.navigate('migrations');
    });

    this.els.navIconSettings.addEventListener('click', (e) => {
      e.preventDefault();
      this.setActiveIcon('settings');
      this.navigate('settings');
    });

    this.els.navLogout.addEventListener('click', (e) => {
      e.preventDefault();
      this.handleLogout();
    });

    // Sidebar new collection button
    this.els.btnSidebarNewCollection.addEventListener('click', () => {
      CollectionsUI.showCreateModal();
    });

    // Collection search
    this.els.collectionsSearch.addEventListener('input', () => {
      this.filterSidebarCollections(this.els.collectionsSearch.value.trim().toLowerCase());
    });

    // Modal close
    this.els.modalClose.addEventListener('click', () => this.closeModal());
    this.els.modalOverlay.addEventListener('click', (e) => {
      if (e.target === this.els.modalOverlay) this.closeModal();
    });

    // Keyboard: Escape closes modal or drawer
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        if (!this.els.drawer.classList.contains('hidden')) {
          this.closeDrawer();
        } else if (!this.els.modalOverlay.classList.contains('hidden')) {
          this.closeModal();
        }
      }
    });

    // Drawer close
    this.els.drawerClose.addEventListener('click', () => this.closeDrawer());
    this.els.drawerOverlay.addEventListener('click', () => this.closeDrawer());
  },

  setActiveIcon(view) {
    const icons = ['nav-icon-collections', 'nav-icon-migrations', 'nav-icon-settings'];
    icons.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle('active', id === 'nav-icon-' + view);
    });
  },

  showCollectionsPanel(show) {
    if (this.els.collectionsPanel) {
      this.els.collectionsPanel.classList.toggle('panel-hidden', !show);
    }
  },

  filterSidebarCollections(query) {
    const items = this.els.collectionsNav.querySelectorAll('.nav-item');
    items.forEach(item => {
      const name = item.querySelector('.truncate');
      if (!name) return;
      const text = name.textContent.toLowerCase();
      item.style.display = (!query || text.includes(query)) ? '' : 'none';
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

      // Collection type icon
      let typeIcon;
      if (col.type === 'auth') {
        typeIcon = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="8" cy="6" r="3"/>
          <path d="M2 14c0-3.3 2.7-5 6-5s6 1.7 6 5"/>
        </svg>`;
      } else if (col.type === 'view') {
        typeIcon = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <rect x="2" y="2" width="5" height="5" rx="0.5"/>
          <rect x="9" y="2" width="5" height="5" rx="0.5"/>
          <rect x="2" y="9" width="5" height="5" rx="0.5"/>
          <rect x="9" y="9" width="5" height="5" rx="0.5"/>
        </svg>`;
      } else {
        typeIcon = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M2 5l6-3 6 3v6l-6 3-6-3V5z"/>
          <path d="M2 5l6 3 6-3"/>
          <path d="M8 8v6"/>
        </svg>`;
      }

      item.innerHTML = `
        <span class="nav-item-icon">${typeIcon}</span>
        <span class="truncate">${this.escapeHtml(col.name)}</span>
      `;

      item.addEventListener('click', (e) => {
        e.preventDefault();
        this.currentCollection = col;
        this.renderSidebarCollections();
        this.setActiveIcon('collections');
        this.navigate('records', col);
      });

      nav.appendChild(item);
    });

    // Re-apply search filter
    const query = this.els.collectionsSearch ? this.els.collectionsSearch.value.trim().toLowerCase() : '';
    if (query) this.filterSidebarCollections(query);
  },

  // ── Navigation / Routing ────────────────────────────────────

  navigate(view, data) {
    this.currentView = view;

    switch (view) {
      case 'collections':
        this.currentCollection = null;
        this.renderSidebarCollections();
        this.setActiveIcon('collections');
        this.showCollectionsPanel(true);
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
        this.setActiveIcon('collections');
        this.showCollectionsPanel(true);
        this.setBreadcrumb([
          { label: 'Collections', onClick: () => this.navigate('collections') },
          { label: this.currentCollection.name, active: true },
        ]);
        // Add inline gear & refresh icons after breadcrumb
        const bcEl = this.els.breadcrumb;
        const iconsSpan = document.createElement('span');
        iconsSpan.className = 'collection-header-icons';
        iconsSpan.innerHTML = `
          <a href="#" class="collection-header-icon" id="btn-bc-settings" title="Collection settings">
            <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="8" cy="8" r="2.5"/>
              <path d="M13.5 8a5.5 5.5 0 01-.3 1.2l1.1.9a.3.3 0 01.1.4l-1.1 1.9a.3.3 0 01-.4.1l-1.3-.5a5 5 0 01-1 .6l-.2 1.4a.3.3 0 01-.3.2H8a.3.3 0 01-.3-.2l-.2-1.4a5 5 0 01-1-.6l-1.3.5a.3.3 0 01-.4-.1L3.7 10.5a.3.3 0 01.1-.4l1.1-.9A5.5 5.5 0 014.5 8c0-.4 0-.8.1-1.2l-1.1-.9a.3.3 0 01-.1-.4l1.1-1.9a.3.3 0 01.4-.1l1.3.5a5 5 0 011-.6l.2-1.4a.3.3 0 01.3-.2H10a.3.3 0 01.3.2l.2 1.4a5 5 0 011 .6l1.3-.5a.3.3 0 01.4.1l1.1 1.9a.3.3 0 01-.1.4l-1.1.9c.1.4.2.8.2 1.2z"/>
            </svg>
          </a>
          <a href="#" class="collection-header-icon" id="btn-bc-refresh" title="Refresh">
            <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M1 1v5h5"/>
              <path d="M1 6a7 7 0 0113.4 2.5M15 15v-5h-5"/>
              <path d="M15 10A7 7 0 011.6 7.5"/>
            </svg>
          </a>
        `;
        bcEl.appendChild(iconsSpan);

        let recordActions = `
          <button class="btn btn-secondary btn-sm" id="btn-api-preview">
            <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4.5 4L1 7l3.5 3"/>
              <path d="M9.5 4L13 7l-3.5 3"/>
            </svg>
            API Preview
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

        // Bind header icons
        this.bindActionEvent('btn-bc-settings', () => {
          CollectionsUI.showDetailModal(this.currentCollection);
        });
        this.bindActionEvent('btn-bc-refresh', () => {
          RecordsUI.loadAndRender(this.currentCollection);
        });
        this.bindActionEvent('btn-api-preview', () => {
          this.showApiPreview(this.currentCollection);
        });
        if (!isView) {
          this.bindActionEvent('btn-new-record', () => {
            RecordsUI.showCreateDrawer(this.currentCollection);
          });
        }
        RecordsUI.loadAndRender(this.currentCollection);
        break;
      }

      case 'migrations':
        this.currentCollection = null;
        this.renderSidebarCollections();
        this.setActiveIcon('migrations');
        this.showCollectionsPanel(false);
        this.setBreadcrumb([{ label: 'Migrations', active: true }]);
        this.setActions('');
        MigrationsUI.loadMigrations();
        break;

      case 'settings':
        this.currentCollection = null;
        this.renderSidebarCollections();
        this.setActiveIcon('settings');
        this.showCollectionsPanel(false);
        this.setBreadcrumb([{ label: 'Settings', active: true }]);
        this.setActions('');
        this.renderSettings();
        break;

      default:
        this.navigate('collections');
    }
  },

  // ── API Preview Modal ─────────────────────────────────────

  showApiPreview(collection) {
    const name = App.escapeHtml(collection.name);
    const baseUrl = window.location.origin;

    const endpoints = [
      {
        id: 'list',
        label: 'List/Search',
        title: 'List/Search (' + name + ')',
        desc: 'Fetch a paginated <strong>' + name + '</strong> records list, supporting sorting and filtering.',
        method: 'GET',
        methodClass: 'api-method-get',
        path: '/api/collections/' + name + '/records',
        note: 'Requires superuser <code class="cell-id">Authorization:TOKEN</code> header',
        params: [
          { name: 'page', type: 'Number', desc: 'The page (aka. offset) of the paginated list (default to 1).' },
          { name: 'perPage', type: 'Number', desc: 'Specify the max returned records per page (default to 30).' },
          { name: 'sort', type: 'String', desc: 'Specify the records order attribute(s). Add <code>-</code> / <code>+</code> (default) in front of the attribute for DESC and ASC order.' },
          { name: 'filter', type: 'String', desc: 'Filter expression to filter/search the returned records list.' },
          { name: 'expand', type: 'String', desc: 'Auto expand record relations.' },
        ],
        code: `<span class="hl-comment">// fetch a paginated records list</span>
<span class="hl-keyword">const</span> <span class="hl-var">resultList</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">getList</span>(1, 50, {
    filter: <span class="hl-string">'someField1 != someField2'</span>,
});

<span class="hl-comment">// you can also fetch all records at once via getFullList</span>
<span class="hl-keyword">const</span> <span class="hl-var">records</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">getFullList</span>({
    sort: <span class="hl-string">'-someField'</span>,
});

<span class="hl-comment">// or fetch only the first record that matches the specified filter</span>
<span class="hl-keyword">const</span> <span class="hl-var">record</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">getFirstListItem</span>(<span class="hl-string">'someField="test"'</span>, {
    expand: <span class="hl-string">'relField1,relField2.subRelField'</span>,
});`,
      },
      {
        id: 'view',
        label: 'View',
        title: 'View (' + name + ')',
        desc: 'Fetch a single <strong>' + name + '</strong> record by its ID.',
        method: 'GET',
        methodClass: 'api-method-get',
        path: '/api/collections/' + name + '/records/:id',
        note: '',
        params: [
          { name: 'expand', type: 'String', desc: 'Auto expand record relations.' },
        ],
        code: `<span class="hl-keyword">const</span> <span class="hl-var">record</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">getOne</span>(<span class="hl-string">'RECORD_ID'</span>, {
    expand: <span class="hl-string">'relField1,relField2.subRelField'</span>,
});`,
      },
      {
        id: 'create',
        label: 'Create',
        title: 'Create (' + name + ')',
        desc: 'Create a new <strong>' + name + '</strong> record.',
        method: 'POST',
        methodClass: 'api-method-post',
        path: '/api/collections/' + name + '/records',
        note: '',
        params: [],
        code: `<span class="hl-keyword">const</span> <span class="hl-var">record</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">create</span>({
    <span class="hl-comment">// ... your data</span>
});`,
      },
      {
        id: 'update',
        label: 'Update',
        title: 'Update (' + name + ')',
        desc: 'Update an existing <strong>' + name + '</strong> record.',
        method: 'PATCH',
        methodClass: 'api-method-patch',
        path: '/api/collections/' + name + '/records/:id',
        note: '',
        params: [],
        code: `<span class="hl-keyword">const</span> <span class="hl-var">record</span> = <span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">update</span>(<span class="hl-string">'RECORD_ID'</span>, {
    <span class="hl-comment">// ... your data</span>
});`,
      },
      {
        id: 'delete',
        label: 'Delete',
        title: 'Delete (' + name + ')',
        desc: 'Delete a single <strong>' + name + '</strong> record.',
        method: 'DELETE',
        methodClass: 'api-method-delete',
        path: '/api/collections/' + name + '/records/:id',
        note: '',
        params: [],
        code: `<span class="hl-keyword">await</span> pb.<span class="hl-func">collection</span>(<span class="hl-string">'${name}'</span>).<span class="hl-func">delete</span>(<span class="hl-string">'RECORD_ID'</span>);`,
      },
    ];

    // Build nav
    const navHtml = endpoints.map((ep, i) =>
      `<button class="drawer-nav-item${i === 0 ? ' active' : ''}" data-api-tab="${ep.id}">${ep.label}</button>`
    ).join('');

    // Build content sections
    const sectionsHtml = endpoints.map((ep, i) => {
      let paramsHtml = '';
      if (ep.params.length > 0) {
        let paramRows = ep.params.map(p =>
          `<tr><td><strong>${p.name}</strong></td><td><span class="badge">${p.type}</span></td><td>${p.desc}</td></tr>`
        ).join('');
        paramsHtml = `
          <h4 class="api-section-title">Query parameters</h4>
          <table class="api-params-table">
            <thead><tr><th>Param</th><th>Type</th><th>Description</th></tr></thead>
            <tbody>${paramRows}</tbody>
          </table>
        `;
      }

      return `
        <div class="api-tab-content${i === 0 ? '' : ' hidden'}" data-api-content="${ep.id}">
          <div style="padding: 1.5rem;">
            <h3 class="api-section-title">${ep.title}</h3>
            <p class="api-section-desc">${ep.desc}</p>
            <div class="api-code-block">${ep.code}</div>
            <div class="api-sdk-link">JavaScript SDK</div>
            <h4 class="api-section-title" style="margin-top: 1.5rem;">API details</h4>
            <div class="api-endpoint-bar">
              <span class="api-method-badge ${ep.methodClass}">${ep.method}</span>
              <span class="api-endpoint-path">${ep.path}</span>
              ${ep.note ? '<span class="api-endpoint-note">' + ep.note + '</span>' : ''}
            </div>
            ${paramsHtml}
          </div>
        </div>
      `;
    }).join('');

    // Compose the drawer body with side nav
    const bodyHtml = `
      <div class="drawer-with-nav" style="flex: 1; overflow: hidden;">
        <div class="drawer-nav">${navHtml}</div>
        <div class="drawer-nav-content">${sectionsHtml}</div>
      </div>
    `;

    this.showDrawer('API Preview', '', '', { wide: true });

    // Replace drawer body with custom layout (need full height nav)
    const drawerBody = this.els.drawerBody;
    drawerBody.style.padding = '0';
    drawerBody.innerHTML = bodyHtml;
    this.els.drawerFooter.style.display = 'none';

    // Bind nav clicks
    drawerBody.querySelectorAll('.drawer-nav-item').forEach(btn => {
      btn.addEventListener('click', () => {
        drawerBody.querySelectorAll('.drawer-nav-item').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        drawerBody.querySelectorAll('[data-api-content]').forEach(sec => sec.classList.add('hidden'));
        const target = drawerBody.querySelector('[data-api-content="' + btn.dataset.apiTab + '"]');
        if (target) target.classList.remove('hidden');
      });
    });

    // Restore footer display on close
    const origClose = this.closeDrawer.bind(this);
    this.closeDrawer = () => {
      this.els.drawerBody.style.padding = '';
      this.els.drawerFooter.style.display = '';
      this.closeDrawer = origClose;
      origClose();
    };
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

  // ── Drawer System ─────────────────────────────────────────

  showDrawer(title, bodyHtml, footerHtml, options = {}) {
    this.els.drawerTitle.innerHTML = title;
    this.els.drawerBody.innerHTML = bodyHtml;
    this.els.drawerFooter.innerHTML = footerHtml || '';
    this.els.drawerHeaderActions.innerHTML = options.headerActions || '';

    const drawer = this.els.drawer;
    if (options.wide) {
      drawer.classList.add('drawer-wide');
    } else {
      drawer.classList.remove('drawer-wide');
    }

    drawer.classList.remove('hidden');
    this.els.drawerOverlay.classList.remove('hidden');

    requestAnimationFrame(() => {
      const firstInput = this.els.drawerBody.querySelector('input, select, textarea');
      if (firstInput) firstInput.focus();
    });
  },

  closeDrawer() {
    this.els.drawer.classList.add('hidden');
    this.els.drawerOverlay.classList.add('hidden');
    this.els.drawerBody.innerHTML = '';
    this.els.drawerFooter.innerHTML = '';
    this.els.drawerHeaderActions.innerHTML = '';
  },

  // ── Selection Bar ──────────────────────────────────────────

  showSelectionBar(count, onReset, onDelete) {
    const bar = this.els.selectionBar;
    bar.innerHTML = `
      <span class="selection-bar-text">Selected ${count} record${count !== 1 ? 's' : ''}</span>
      <button class="selection-bar-reset" id="selection-reset">Reset</button>
      <span class="selection-bar-spacer"></span>
      <button class="selection-bar-delete" id="selection-delete">Delete selected</button>
    `;
    bar.classList.remove('hidden');
    document.getElementById('selection-reset').addEventListener('click', onReset);
    document.getElementById('selection-delete').addEventListener('click', onDelete);
  },

  hideSelectionBar() {
    this.els.selectionBar.classList.add('hidden');
    this.els.selectionBar.innerHTML = '';
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
