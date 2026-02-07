/**
 * PPBase Admin - Collections UI
 *
 * Handles rendering the collections list, collection detail/schema view,
 * and create/edit/delete collection modals.
 * Supports all 14 PocketBase field types with full options configuration.
 *
 * Depends on:
 *   - PBClient (api.js)  -- API client
 *   - App      (app.js)  -- Application controller
 */
const CollectionsUI = {

  // ── SQL Editor State ─────────────────────────────────────
  _dbTables: null,  // cached table metadata

  // ── Field type definitions ─────────────────────────────────

  fieldTypes: [
    'text', 'editor', 'number', 'bool',
    'email', 'url', 'date', 'select',
    'json', 'file', 'relation',
  ],

  fieldTypeConfig: {
    text:     { label: 'Plain text',  icon: 'T',  bg: '#eff6ff', color: '#1d4ed8' },
    editor:   { label: 'Rich editor', icon: 'R',  bg: '#f8fafc', color: '#475569' },
    number:   { label: 'Number',      icon: '#',  bg: '#f5f3ff', color: '#7c3aed' },
    bool:     { label: 'Bool',        icon: 'B',  bg: '#ecfdf5', color: '#059669' },
    email:    { label: 'Email',       icon: '@',  bg: '#eef2ff', color: '#4f46e5' },
    url:      { label: 'URL',         icon: 'U',  bg: '#eef2ff', color: '#4f46e5' },
    date:     { label: 'Datetime',    icon: 'D',  bg: '#fffbeb', color: '#d97706' },
    select:   { label: 'Select',      icon: 'S',  bg: '#fdf4ff', color: '#a855f7' },
    json:     { label: 'JSON',        icon: 'J',  bg: '#f0fdf4', color: '#16a34a' },
    file:     { label: 'File',        icon: 'F',  bg: '#fff7ed', color: '#ea580c' },
    relation: { label: 'Relation',    icon: 'L',  bg: '#fef2f2', color: '#dc2626' },
  },

  fieldTypeBadgeClass(type) {
    const map = {
      text: 'badge-blue',
      number: 'badge-purple',
      bool: 'badge-green',
      email: 'badge-indigo',
      url: 'badge-indigo',
      date: 'badge-yellow',
      select: 'badge-purple',
      json: 'badge-green',
      file: 'badge-yellow',
      relation: 'badge-red',
      editor: '',
    };
    return map[type] || '';
  },

  // ── Render Collections List ────────────────────────────────

  renderList(collections) {
    const body = App.els.contentBody;

    if (!collections || collections.length === 0) {
      body.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="3" width="7" height="7" rx="1"/>
              <rect x="14" y="3" width="7" height="7" rx="1"/>
              <rect x="3" y="14" width="7" height="7" rx="1"/>
              <rect x="14" y="14" width="7" height="7" rx="1"/>
            </svg>
          </div>
          <h3>No collections yet</h3>
          <p>Create your first collection to start organizing your data.</p>
          <button class="btn btn-primary" id="btn-empty-new-collection">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M7 1v12M1 7h12"/></svg>
            New collection
          </button>
        </div>
      `;
      App.bindActionEvent('btn-empty-new-collection', () => this.showCreateModal());
      return;
    }

    let rows = '';
    collections.forEach((col) => {
      const fieldCount = ((col.fields || col.schema) && (col.fields || col.schema).length) || 0;
      let typeBadge;
      if (col.type === 'auth') {
        typeBadge = '<span class="badge badge-green">auth</span>';
      } else if (col.type === 'view') {
        typeBadge = '<span class="badge badge-yellow">view</span>';
      } else {
        typeBadge = '<span class="badge badge-blue">base</span>';
      }

      rows += `
        <tr class="collection-row" data-id="${App.escapeHtml(col.id)}" data-name="${App.escapeHtml(col.name)}">
          <td class="font-medium">${App.escapeHtml(col.name)}</td>
          <td>${typeBadge}</td>
          <td class="text-muted">${fieldCount} field${fieldCount !== 1 ? 's' : ''}</td>
          <td class="text-muted text-sm">${App.formatDate(col.created)}</td>
          <td class="row-actions">
            <button class="btn btn-ghost btn-sm btn-view-collection" title="View records">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
                <path d="M5 7h8M10 4l3 3-3 3"/>
              </svg>
            </button>
          </td>
        </tr>
      `;
    });

    body.innerHTML = `
      <div class="content-padded">
        <div class="table-wrapper" style="border: 1px solid #e2e8f0; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
          <table class="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Fields</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>
    `;

    body.querySelectorAll('.collection-row').forEach((row) => {
      const name = row.dataset.name;
      const col = collections.find((c) => c.name === name);

      row.addEventListener('click', (e) => {
        if (e.target.closest('.btn-view-collection')) return;
        App.currentCollection = col;
        App.renderSidebarCollections();
        App.navigate('records', col);
      });

      const viewBtn = row.querySelector('.btn-view-collection');
      if (viewBtn) {
        viewBtn.addEventListener('click', () => {
          App.currentCollection = col;
          App.renderSidebarCollections();
          App.navigate('records', col);
        });
      }
    });
  },

  // ── Create Collection Drawer ────────────────────────────────

  showCreateModal() {
    const bodyHtml = `
      <div class="form-group">
        <label class="form-label">Name <span class="text-light">*</span></label>
        <div style="display: flex; align-items: center; gap: 0.75rem;">
          <input class="form-input" type="text" id="col-name" placeholder='eg. "posts"' required style="flex: 1;">
          <select class="form-select" id="col-type" style="flex: 0 0 auto; min-width: 140px;">
            <option value="base">Type: Base</option>
            <option value="auth">Type: Auth</option>
            <option value="view">Type: View</option>
          </select>
        </div>
      </div>
      <div id="col-tabs" class="mt-4">
        <div style="display: flex; border-bottom: 2px solid #e2e8f0; margin-bottom: 1rem;">
          <button class="btn btn-ghost col-tab-btn active" data-tab="fields" id="tab-fields" style="border-radius: 0; border-bottom: 2px solid #4f46e5; margin-bottom: -2px;">Fields</button>
          <button class="btn btn-ghost col-tab-btn" data-tab="api-rules" id="tab-api-rules" style="border-radius: 0; margin-bottom: -2px;">API Rules</button>
          <button class="btn btn-ghost col-tab-btn hidden" data-tab="options" id="tab-options" style="border-radius: 0; margin-bottom: -2px;">Options</button>
          <button class="btn btn-ghost col-tab-btn hidden" data-tab="query" id="tab-query" style="border-radius: 0; margin-bottom: -2px;">Query</button>
        </div>
        <div class="col-tab-content" data-tab-content="fields" id="tab-content-fields">
          <div id="schema-section">
            <div id="schema-fields-container"></div>
            <button type="button" class="btn btn-secondary w-full mt-3" id="btn-add-field" style="border-style: dashed;">
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 1v10M1 6h10"/></svg>
              New field
            </button>
          </div>
        </div>
        <div class="col-tab-content hidden" data-tab-content="api-rules" id="tab-content-api-rules">
          <div class="form-group">
            <label class="form-label">List/Search rule</label>
            <input class="form-input" type="text" id="col-rule-list" placeholder='Leave empty for public access, or enter a filter expression'>
          </div>
          <div class="form-group mt-3">
            <label class="form-label">View rule</label>
            <input class="form-input" type="text" id="col-rule-view" placeholder='Leave empty for public access'>
          </div>
          <div class="form-group mt-3">
            <label class="form-label">Create rule</label>
            <input class="form-input" type="text" id="col-rule-create" placeholder='Leave empty for public access'>
          </div>
          <div class="form-group mt-3">
            <label class="form-label">Update rule</label>
            <input class="form-input" type="text" id="col-rule-update" placeholder='Leave empty for public access'>
          </div>
          <div class="form-group mt-3">
            <label class="form-label">Delete rule</label>
            <input class="form-input" type="text" id="col-rule-delete" placeholder='Leave empty for public access'>
          </div>
          <span class="form-help mt-2">Set to <code class="cell-id">null</code> (leave blank) for admin-only, or an empty string for public. Use filter expressions to control access.</span>
        </div>
        <div class="col-tab-content hidden" data-tab-content="options" id="tab-content-options">
          <p class="text-muted text-sm">Auth collection options will appear here.</p>
        </div>
        <div class="col-tab-content hidden" data-tab-content="query" id="tab-content-query">
          <div class="form-group">
            <label class="form-label">Select query <span class="text-light">*</span></label>
            <div id="col-view-query-editor"></div>
            <span class="form-help">SQL SELECT query that defines this view. Must include id, created, and updated columns. Press <kbd>Ctrl+Space</kbd> for autocomplete.</span>
          </div>
          <ul class="text-sm text-muted mt-3" style="padding-left: 1.25rem; line-height: 1.8;">
            <li>Wildcard columns (<code class="cell-id">*</code>) are not supported.</li>
            <li>The query must have a unique <code class="cell-id">id</code> column.<br>If your query doesn't have a suitable one, you can use the universal <code class="cell-id">(ROW_NUMBER() OVER()) as id</code>.</li>
            <li>Expressions must be aliased with a valid formatted field name, e.g. <code class="cell-id">MAX(balance) as maxBalance</code>.</li>
            <li>Combined/multi-spaced expressions must be wrapped in parenthesis, e.g.<br><code class="cell-id">(MAX(balance) + 1) as maxBalance</code>.</li>
          </ul>
        </div>
      </div>
    `;

    const footerHtml = `
      <button class="btn btn-secondary" id="drawer-cancel">Cancel</button>
      <button class="btn btn-primary" id="drawer-save">Create</button>
    `;

    App.showDrawer('New collection', bodyHtml, footerHtml);

    // Initialize SQL editor
    const sqlEditor = this.createSqlEditor();
    document.getElementById('col-view-query-editor').appendChild(sqlEditor.container);
    this._currentSqlEditor = sqlEditor;

    // Tab switching
    this._initTabs();

    // Type change handler
    const typeSelect = document.getElementById('col-type');
    typeSelect.addEventListener('change', () => {
      const t = typeSelect.value;
      const tabFields = document.getElementById('tab-fields');
      const tabQuery = document.getElementById('tab-query');
      const tabOptions = document.getElementById('tab-options');

      if (t === 'view') {
        tabFields.classList.add('hidden');
        tabQuery.classList.remove('hidden');
        tabOptions.classList.add('hidden');
        // Auto-switch to query tab
        tabQuery.click();
      } else {
        tabFields.classList.remove('hidden');
        tabQuery.classList.add('hidden');
        if (t === 'auth') {
          tabOptions.classList.remove('hidden');
        } else {
          tabOptions.classList.add('hidden');
        }
        tabFields.click();

        // Update system fields display
        const schemaContainer = document.getElementById('schema-fields-container');
        const oldSystemFields = schemaContainer.querySelector('[data-role="system-fields"]');
        if (oldSystemFields) oldSystemFields.remove();
        // Re-add at the beginning
        const firstChild = schemaContainer.firstChild;
        const tempDiv = document.createElement('div');
        schemaContainer.insertBefore(tempDiv, firstChild);
        this._addSystemFieldsDisplay(schemaContainer, t);
        // Move the system fields before any user fields
        const newSystemFields = schemaContainer.querySelector('[data-role="system-fields"]');
        if (newSystemFields && firstChild) {
          schemaContainer.insertBefore(newSystemFields, schemaContainer.querySelector('[data-role="user-fields"]') || firstChild);
        }
        tempDiv.remove();
      }
    });

    // Bind add-field to show type picker
    document.getElementById('btn-add-field').addEventListener('click', () => {
      const schemaContainer = document.getElementById('schema-fields-container');
      this.showTypePicker(schemaContainer, (type) => {
        this.addFieldRow(schemaContainer, null, type);
      });
    });

    // Add default system fields display
    const container = document.getElementById('schema-fields-container');
    this._addSystemFieldsDisplay(container, 'base');

    // Bind cancel/save
    document.getElementById('drawer-cancel').addEventListener('click', () => App.closeDrawer());
    document.getElementById('drawer-save').addEventListener('click', () => this.handleCreate());
  },

  _initTabs() {
    document.querySelectorAll('.col-tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.col-tab-btn').forEach(b => {
          b.classList.remove('active');
          b.style.borderBottom = '2px solid transparent';
        });
        btn.classList.add('active');
        btn.style.borderBottom = '2px solid #4f46e5';
        document.querySelectorAll('.col-tab-content').forEach(c => c.classList.add('hidden'));
        const target = document.querySelector('[data-tab-content="' + btn.dataset.tab + '"]');
        if (target) target.classList.remove('hidden');
      });
    });
  },

  _addSystemFieldsDisplay(container, type) {
    const cfg = this.fieldTypeConfig;

    const makeRow = (icon, bg, color, name, badges = '') => {
      return `
        <div class="field-editor-row" style="color: #94a3b8;">
          <span class="field-row-icon" style="background: ${bg}; color: ${color};">${icon}</span>
          <span class="field-row-name" style="color: #94a3b8;">${name}</span>
          ${badges ? `<span class="field-row-badges">${badges}</span>` : ''}
        </div>
      `;
    };

    let rows = makeRow('T', cfg.text.bg, cfg.text.color, 'id',
      '<span class="badge badge-green">Nonempty</span>');

    if (type === 'auth') {
      rows += makeRow('P', '#f8fafc', '#475569', 'password',
        '<span class="badge badge-green">Nonempty</span><span class="badge badge-red">Hidden</span>');
      rows += makeRow('T', cfg.text.bg, cfg.text.color, 'tokenKey',
        '<span class="badge badge-green">Nonempty</span><span class="badge badge-red">Hidden</span>');
      rows += makeRow('@', cfg.email.bg, cfg.email.color, 'email',
        '<span class="badge badge-green">Nonempty</span>');
      rows += makeRow('B', cfg.bool.bg, cfg.bool.color, 'emailVisibility', '');
      rows += makeRow('B', cfg.bool.bg, cfg.bool.color, 'verified', '');
    }

    rows += `
      <div class="field-editor-row">
        <span class="field-row-icon" style="background: ${cfg.date.bg}; color: ${cfg.date.color};">D</span>
        <span class="field-row-name">created</span>
        <span class="field-row-info">
          <select class="form-select" style="font-size: 0.75rem; padding: 0.125rem 1.5rem 0.125rem 0.375rem; min-height: 22px; border-radius: 4px;" disabled>
            <option>Create</option>
          </select>
        </span>
      </div>
      <div class="field-editor-row">
        <span class="field-row-icon" style="background: ${cfg.date.bg}; color: ${cfg.date.color};">D</span>
        <span class="field-row-name">updated</span>
        <span class="field-row-info">
          <select class="form-select" style="font-size: 0.75rem; padding: 0.125rem 1.5rem 0.125rem 0.375rem; min-height: 22px; border-radius: 4px;" disabled>
            <option>Create/Update</option>
          </select>
        </span>
      </div>
    `;

    const systemFields = document.createElement('div');
    systemFields.className = 'fields-container';
    systemFields.setAttribute('data-role', 'system-fields');
    systemFields.innerHTML = rows;
    container.appendChild(systemFields);
  },

  // ── Type Picker Grid ──────────────────────────────────────

  showTypePicker(container, onSelect) {
    // Remove existing picker if any
    const existing = container.parentElement.querySelector('.field-type-picker');
    if (existing) { existing.remove(); return; }

    const picker = document.createElement('div');
    picker.className = 'field-type-picker';

    let items = '';
    this.fieldTypes.forEach(type => {
      const cfg = this.fieldTypeConfig[type];
      items += `
        <div class="field-type-picker-item" data-type="${type}">
          <span class="type-icon" style="background: ${cfg.bg}; color: ${cfg.color};">${cfg.icon}</span>
          ${cfg.label}
        </div>
      `;
    });

    picker.innerHTML = `<div class="field-type-picker-grid">${items}</div>`;

    picker.querySelectorAll('.field-type-picker-item').forEach(item => {
      item.addEventListener('click', () => {
        const type = item.dataset.type;
        picker.remove();
        onSelect(type);
      });
    });

    // Insert picker before the "New field" button
    const addBtn = container.parentElement.querySelector('#btn-add-field, #edit-btn-add-field');
    if (addBtn) {
      addBtn.parentElement.insertBefore(picker, addBtn);
    } else {
      container.parentElement.appendChild(picker);
    }
  },

  // ── Field Row with Compact Layout ───────────────────────────

  _getFieldInfoText(type, field) {
    if (!field) return '';
    const opts = field.options || {};
    switch (type) {
      case 'relation': {
        const cid = opts.collectionId || field.collectionId || '';
        if (cid && App.collections) {
          const rel = App.collections.find(c => c.id === cid);
          if (rel) return rel.name;
        }
        return cid ? cid : '';
      }
      case 'select': {
        const vals = opts.values || field.values || [];
        return vals.length ? vals.slice(0, 3).join(', ') + (vals.length > 3 ? '...' : '') : '';
      }
      default:
        return '';
    }
  },

  addFieldRow(container, existingField, fieldType) {
    const type = fieldType || (existingField ? (existingField.type || 'text') : 'text');
    const cfg = this.fieldTypeConfig[type] || this.fieldTypeConfig.text;
    const name = existingField ? (existingField.name || '') : '';
    const required = existingField && existingField.required;
    const infoText = this._getFieldInfoText(type, existingField);

    // Build the wrapper that holds the row + optional expanded options
    const wrapper = document.createElement('div');
    wrapper.className = 'field-row-wrapper';
    wrapper.dataset.fieldType = type;

    // Compact row
    const row = document.createElement('div');
    row.className = 'field-editor-row';
    row.innerHTML = `
      <span class="field-row-icon" style="background: ${cfg.bg}; color: ${cfg.color};">${cfg.icon}</span>
      <span class="field-row-name">
        <input type="text" placeholder="Field name" data-role="field-name" value="${App.escapeHtml(name)}">
      </span>
      ${infoText ? `<span class="field-row-info">${App.escapeHtml(infoText)}</span>` : ''}
      ${required ? '<span class="field-row-badges"><span class="badge badge-green">Required</span></span>' : ''}
      <button type="button" class="field-row-gear" title="Field settings" data-role="toggle-options">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="8" cy="8" r="2"/>
          <path d="M13.5 8a5.5 5.5 0 01-.2 1.1l1 .8a.3.3 0 01.1.4l-1 1.7a.3.3 0 01-.4.1l-1.2-.5a5 5 0 01-1 .6l-.2 1.3a.3.3 0 01-.3.2H7.7a.3.3 0 01-.3-.2l-.2-1.3a5 5 0 01-1-.6l-1.2.5a.3.3 0 01-.4-.1l-1-1.7a.3.3 0 01.1-.4l1-.8A5.5 5.5 0 014.5 8c0-.4 0-.7.1-1.1l-1-.8a.3.3 0 01-.1-.4l1-1.7a.3.3 0 01.4-.1l1.2.5a5 5 0 011-.6l.2-1.3A.3.3 0 017.7 2h1.6a.3.3 0 01.3.2l.2 1.3a5 5 0 011 .6l1.2-.5a.3.3 0 01.4.1l1 1.7a.3.3 0 01-.1.4l-1 .8c.1.4.2.7.2 1.1z"/>
        </svg>
      </button>
      <button type="button" class="field-row-remove" title="Remove field" data-role="remove-field">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M3 3l8 8M11 3l-8 8"/></svg>
      </button>
    `;

    wrapper.appendChild(row);

    // Hidden data elements for type and required
    const hiddenType = document.createElement('input');
    hiddenType.type = 'hidden';
    hiddenType.dataset.role = 'field-type';
    hiddenType.value = type;
    wrapper.appendChild(hiddenType);

    const hiddenRequired = document.createElement('input');
    hiddenRequired.type = 'hidden';
    hiddenRequired.dataset.role = 'field-required';
    hiddenRequired.value = required ? '1' : '0';
    wrapper.appendChild(hiddenRequired);

    // Options panel element (hidden until gear is clicked)
    const optionsWrapper = document.createElement('div');
    optionsWrapper.className = 'field-options-wrapper hidden';
    optionsWrapper.dataset.role = 'field-options-wrapper';
    wrapper.appendChild(optionsWrapper);

    // Gear toggle
    const gearBtn = row.querySelector('[data-role="toggle-options"]');
    gearBtn.addEventListener('click', () => {
      const isHidden = optionsWrapper.classList.contains('hidden');
      if (isHidden) {
        this._renderExpandedOptions(optionsWrapper, type, existingField, hiddenRequired, row);
        optionsWrapper.classList.remove('hidden');
        gearBtn.classList.add('active');
      } else {
        optionsWrapper.classList.add('hidden');
        gearBtn.classList.remove('active');
      }
    });

    // Remove button
    row.querySelector('[data-role="remove-field"]').addEventListener('click', () => wrapper.remove());

    // Append to the user-fields container
    let fieldsBox = container.querySelector('[data-role="user-fields"]');
    if (!fieldsBox) {
      fieldsBox = document.createElement('div');
      fieldsBox.className = 'fields-container mt-2';
      fieldsBox.setAttribute('data-role', 'user-fields');
      container.appendChild(fieldsBox);
    }
    fieldsBox.appendChild(wrapper);

    if (!existingField) {
      row.querySelector('[data-role="field-name"]').focus();
      // Auto-expand options for newly created fields
      this._renderExpandedOptions(optionsWrapper, type, existingField, hiddenRequired, row);
      optionsWrapper.classList.remove('hidden');
      gearBtn.classList.add('active');
    }

    // If editing existing, pre-populate the options in background
    if (existingField) {
      this._renderExpandedOptions(optionsWrapper, type, existingField, hiddenRequired, row);
      // Keep collapsed - options are rendered but wrapper stays hidden
      // This ensures gatherSchemaFields can read data-opt values
    }
  },

  _renderExpandedOptions(wrapper, type, existingField, hiddenRequired, row) {
    if (wrapper.dataset.rendered === '1') return;
    wrapper.dataset.rendered = '1';

    const required = hiddenRequired.value === '1';

    let html = `
      <div style="margin-bottom: 0.75rem;">
        <label class="form-checkbox" style="min-height: auto;">
          <input type="checkbox" data-role="field-required-check" ${required ? 'checked' : ''}>
          <span class="text-sm">Required</span>
        </label>
      </div>
      <div data-role="field-options"></div>
    `;

    wrapper.innerHTML = html;

    // Bind required checkbox to hidden + badge
    const reqCheck = wrapper.querySelector('[data-role="field-required-check"]');
    reqCheck.addEventListener('change', () => {
      hiddenRequired.value = reqCheck.checked ? '1' : '0';
      // Update badge
      let badges = row.querySelector('.field-row-badges');
      if (reqCheck.checked) {
        if (!badges) {
          badges = document.createElement('span');
          badges.className = 'field-row-badges';
          const gear = row.querySelector('[data-role="toggle-options"]');
          row.insertBefore(badges, gear);
        }
        if (!badges.querySelector('.badge')) {
          badges.innerHTML = '<span class="badge badge-green">Required</span>';
        }
      } else {
        if (badges) badges.remove();
      }
    });

    // Render type-specific options
    const optionsPanel = wrapper.querySelector('[data-role="field-options"]');
    this.renderFieldOptions(type, optionsPanel);

    if (existingField) {
      this._populateFieldOptions(optionsPanel, existingField);
    }
  },

  /**
   * Populate rendered option inputs with values from an existing field.
   */
  _populateFieldOptions(panel, field) {
    const opts = field.options || {};

    // Helper: set value on input/select by data-opt attribute
    const setOpt = (key, value) => {
      if (value == null || value === '') return;
      const el = panel.querySelector('[data-opt="' + key + '"]');
      if (!el) return;
      if (el.type === 'checkbox') {
        el.checked = !!value;
      } else if (el.classList.contains('tags-input-wrapper')) {
        // Tags input: add each value as a tag
        const values = Array.isArray(value) ? value : [value];
        values.forEach((v) => this.addTag(el, String(v)));
      } else {
        el.value = value;
      }
    };

    // Flat format keys (from API response)
    const allKeys = { ...opts };
    // Also check top-level field keys for flat format
    for (const k of Object.keys(field)) {
      if (!['name', 'type', 'required', 'id', 'system', 'hidden', 'presentable', 'options'].includes(k)) {
        allKeys[k] = field[k];
      }
    }

    for (const [key, value] of Object.entries(allKeys)) {
      // Special handling for tags-input-wrapper (values, mimeTypes)
      const wrapper = panel.querySelector('[data-opt="' + key + '"].tags-input-wrapper');
      if (wrapper && Array.isArray(value)) {
        value.forEach((v) => this.addTag(wrapper, String(v)));
        continue;
      }
      setOpt(key, value);
    }
  },

  // ── Render Field Type Options ──────────────────────────────

  renderFieldOptions(type, container) {
    let html = '';

    switch (type) {
      case 'text':
        html = `
          <div class="field-options-grid">
            <div class="form-group">
              <label class="form-label">Min length</label>
              <input class="form-input" type="number" data-opt="min" placeholder="0" min="0">
            </div>
            <div class="form-group">
              <label class="form-label">Max length</label>
              <input class="form-input" type="number" data-opt="max" placeholder="5000" min="0">
            </div>
            <div class="form-group" style="grid-column: span 2;">
              <label class="form-label">Pattern <span class="form-label-hint">(regex)</span></label>
              <input class="form-input" type="text" data-opt="pattern" placeholder="e.g. ^[a-z]+$">
            </div>
          </div>
        `;
        break;

      case 'number':
        html = `
          <div class="field-options-grid">
            <div class="form-group">
              <label class="form-label">Min value</label>
              <input class="form-input" type="number" step="any" data-opt="min" placeholder="No limit">
            </div>
            <div class="form-group">
              <label class="form-label">Max value</label>
              <input class="form-input" type="number" step="any" data-opt="max" placeholder="No limit">
            </div>
            <div class="form-group">
              <label class="form-checkbox">
                <input type="checkbox" data-opt="onlyInt">
                <span>Integer only</span>
              </label>
            </div>
          </div>
        `;
        break;

      case 'select':
        html = `
          <div class="field-options-stack">
            <div class="form-group">
              <label class="form-label">Values</label>
              <div class="tags-input-wrapper" data-opt="values">
                <div class="tags-container"></div>
                <input class="tags-input" type="text" placeholder="Type a value and press Enter">
              </div>
              <span class="form-help">Press Enter or comma to add a value. Click a value to remove it.</span>
            </div>
            <div class="form-group">
              <label class="form-label">Max select</label>
              <select class="form-select" data-opt="maxSelect" style="max-width: 200px;">
                <option value="1">Single (1)</option>
                <option value="2">Multiple (2)</option>
                <option value="3">Multiple (3)</option>
                <option value="5">Multiple (5)</option>
                <option value="10">Multiple (10)</option>
                <option value="999">Unlimited</option>
              </select>
              <span class="form-help">Single allows only 1 value; multiple stores as array.</span>
            </div>
          </div>
        `;
        break;

      case 'relation':
        html = `
          <div class="field-options-stack">
            <div class="form-group">
              <label class="form-label">Related collection</label>
              <select class="form-select" data-opt="collectionId">
                <option value="">-- Select a collection --</option>
                ${(App.collections || []).map((c) =>
                  `<option value="${App.escapeHtml(c.id)}">${App.escapeHtml(c.name)}</option>`
                ).join('')}
              </select>
              <span class="form-help">The collection this field links to. Cannot be changed after creation.</span>
            </div>
            <div class="field-options-grid">
              <div class="form-group">
                <label class="form-label">Max select</label>
                <select class="form-select" data-opt="maxSelect">
                  <option value="1">Single (1)</option>
                  <option value="5">Multiple (5)</option>
                  <option value="10">Multiple (10)</option>
                  <option value="999">Unlimited</option>
                </select>
              </div>
              <div class="form-group" style="display: flex; align-items: flex-end;">
                <label class="form-checkbox">
                  <input type="checkbox" data-opt="cascadeDelete">
                  <span>Cascade delete</span>
                </label>
              </div>
            </div>
          </div>
        `;
        break;

      case 'file':
        html = `
          <div class="field-options-stack">
            <div class="field-options-grid">
              <div class="form-group">
                <label class="form-label">Max file size</label>
                <select class="form-select" data-opt="maxSize">
                  <option value="1048576">1 MB</option>
                  <option value="5242880" selected>5 MB</option>
                  <option value="10485760">10 MB</option>
                  <option value="52428800">50 MB</option>
                  <option value="104857600">100 MB</option>
                </select>
              </div>
              <div class="form-group">
                <label class="form-label">Max files</label>
                <select class="form-select" data-opt="maxSelect">
                  <option value="1">Single (1)</option>
                  <option value="5">Multiple (5)</option>
                  <option value="10">Multiple (10)</option>
                  <option value="99">Multiple (99)</option>
                </select>
              </div>
            </div>
            <div class="form-group">
              <label class="form-label">Allowed MIME types <span class="form-label-hint">(empty = all)</span></label>
              <div class="tags-input-wrapper" data-opt="mimeTypes">
                <div class="tags-container"></div>
                <input class="tags-input" type="text" placeholder="e.g. image/jpeg, application/pdf">
              </div>
              <div class="form-help flex gap-2 mt-1">
                <button type="button" class="btn btn-ghost btn-sm mime-preset" data-preset="image/jpeg,image/png,image/webp,image/gif">Images</button>
                <button type="button" class="btn btn-ghost btn-sm mime-preset" data-preset="application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document">Documents</button>
                <button type="button" class="btn btn-ghost btn-sm mime-preset" data-preset="video/mp4,video/webm">Videos</button>
              </div>
            </div>
          </div>
        `;
        break;

      case 'email':
      case 'url':
        html = `
          <div class="field-options-grid">
            <div class="form-group" style="grid-column: span 2;">
              <label class="form-label">Only domains <span class="form-label-hint">(allowlist)</span></label>
              <input class="form-input" type="text" data-opt="onlyDomains" placeholder="e.g. example.com, company.org">
              <span class="form-help">Comma-separated. Leave empty to allow all domains.</span>
            </div>
            <div class="form-group" style="grid-column: span 2;">
              <label class="form-label">Except domains <span class="form-label-hint">(blocklist)</span></label>
              <input class="form-input" type="text" data-opt="exceptDomains" placeholder="e.g. spam.com, test.org">
              <span class="form-help">Comma-separated. Mutually exclusive with "only domains".</span>
            </div>
          </div>
        `;
        break;

      case 'date':
        html = `
          <div class="field-options-grid">
            <div class="form-group">
              <label class="form-label">Min date</label>
              <input class="form-input" type="datetime-local" data-opt="min">
            </div>
            <div class="form-group">
              <label class="form-label">Max date</label>
              <input class="form-input" type="datetime-local" data-opt="max">
            </div>
          </div>
        `;
        break;

      case 'json':
        html = `
          <div class="field-options-grid">
            <div class="form-group">
              <label class="form-label">Max size (bytes)</label>
              <input class="form-input" type="number" data-opt="maxSize" placeholder="1048576" min="1">
              <span class="form-help">Default: 1 MB (1048576 bytes).</span>
            </div>
          </div>
        `;
        break;

      case 'editor':
        html = `
          <div class="field-options-grid">
            <div class="form-group">
              <label class="form-label">Max size (bytes)</label>
              <input class="form-input" type="number" data-opt="maxSize" placeholder="5242880" min="1">
              <span class="form-help">Default: 5 MB (5242880 bytes).</span>
            </div>
            <div class="form-group" style="display: flex; align-items: flex-end;">
              <label class="form-checkbox">
                <input type="checkbox" data-opt="convertURLs">
                <span>Convert URLs</span>
              </label>
            </div>
          </div>
        `;
        break;

      // bool has no extra options
      case 'bool':
      default:
        html = '';
        break;
    }

    container.innerHTML = html;

    // Initialize tags inputs
    container.querySelectorAll('.tags-input-wrapper').forEach((wrapper) => {
      this.initTagsInput(wrapper);
    });

    // Initialize MIME preset buttons
    container.querySelectorAll('.mime-preset').forEach((btn) => {
      btn.addEventListener('click', () => {
        const wrapper = btn.closest('.form-group').querySelector('.tags-input-wrapper');
        if (wrapper) {
          const values = btn.dataset.preset.split(',');
          values.forEach((v) => this.addTag(wrapper, v.trim()));
        }
      });
    });
  },

  // ── Tags Input (for select values, mimeTypes) ─────────────

  initTagsInput(wrapper) {
    const input = wrapper.querySelector('.tags-input');
    const tagsContainer = wrapper.querySelector('.tags-container');

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        const val = input.value.trim().replace(/,$/, '').trim();
        if (val) {
          this.addTag(wrapper, val);
          input.value = '';
        }
      }
      if (e.key === 'Backspace' && !input.value) {
        const lastTag = tagsContainer.querySelector('.tag:last-child');
        if (lastTag) lastTag.remove();
      }
    });

    input.addEventListener('blur', () => {
      const val = input.value.trim().replace(/,$/, '').trim();
      if (val) {
        this.addTag(wrapper, val);
        input.value = '';
      }
    });
  },

  addTag(wrapper, value) {
    const tagsContainer = wrapper.querySelector('.tags-container');
    // Avoid duplicates
    const existing = tagsContainer.querySelectorAll('.tag');
    for (const tag of existing) {
      if (tag.dataset.value === value) return;
    }

    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.dataset.value = value;
    tag.innerHTML = `
      ${App.escapeHtml(value)}
      <button type="button" class="tag-remove" title="Remove">&times;</button>
    `;
    tag.querySelector('.tag-remove').addEventListener('click', () => tag.remove());
    tagsContainer.appendChild(tag);
  },

  getTagValues(wrapper) {
    const tags = wrapper.querySelectorAll('.tag');
    return Array.from(tags).map((t) => t.dataset.value);
  },

  // ── Gather Schema Fields with Options ──────────────────────

  gatherSchemaFields() {
    const wrappers = document.querySelectorAll('.field-row-wrapper');
    const schema = [];

    wrappers.forEach((wrapper) => {
      const name = wrapper.querySelector('[data-role="field-name"]').value.trim();
      const type = wrapper.querySelector('[data-role="field-type"]').value;
      const required = wrapper.querySelector('[data-role="field-required"]').value === '1';

      if (!name) return;

      // Build field object (flat PocketBase v0.23+ format)
      const field = { name, type, required };

      // Gather type-specific options from the options panel
      const optionsWrapper = wrapper.querySelector('[data-role="field-options-wrapper"]');
      const optionsPanel = optionsWrapper ? optionsWrapper.querySelector('[data-role="field-options"]') : null;
      if (!optionsPanel) {
        schema.push(field);
        return;
      }

      switch (type) {
        case 'text': {
          const min = optionsPanel.querySelector('[data-opt="min"]');
          const max = optionsPanel.querySelector('[data-opt="max"]');
          const pattern = optionsPanel.querySelector('[data-opt="pattern"]');
          if (min && min.value) field.min = parseInt(min.value, 10);
          if (max && max.value) field.max = parseInt(max.value, 10);
          if (pattern && pattern.value) field.pattern = pattern.value;
          break;
        }

        case 'number': {
          const min = optionsPanel.querySelector('[data-opt="min"]');
          const max = optionsPanel.querySelector('[data-opt="max"]');
          const onlyInt = optionsPanel.querySelector('[data-opt="onlyInt"]');
          if (min && min.value !== '') field.min = parseFloat(min.value);
          if (max && max.value !== '') field.max = parseFloat(max.value);
          if (onlyInt && onlyInt.checked) field.onlyInt = true;
          break;
        }

        case 'select': {
          const valuesWrapper = optionsPanel.querySelector('[data-opt="values"]');
          const maxSelect = optionsPanel.querySelector('[data-opt="maxSelect"]');
          if (valuesWrapper) field.values = this.getTagValues(valuesWrapper);
          if (maxSelect) field.maxSelect = parseInt(maxSelect.value, 10);
          break;
        }

        case 'relation': {
          const collectionId = optionsPanel.querySelector('[data-opt="collectionId"]');
          const maxSelect = optionsPanel.querySelector('[data-opt="maxSelect"]');
          const cascadeDelete = optionsPanel.querySelector('[data-opt="cascadeDelete"]');
          if (collectionId && collectionId.value) field.collectionId = collectionId.value;
          if (maxSelect) field.maxSelect = parseInt(maxSelect.value, 10);
          if (cascadeDelete && cascadeDelete.checked) field.cascadeDelete = true;
          break;
        }

        case 'file': {
          const maxSize = optionsPanel.querySelector('[data-opt="maxSize"]');
          const maxSelect = optionsPanel.querySelector('[data-opt="maxSelect"]');
          const mimeWrapper = optionsPanel.querySelector('[data-opt="mimeTypes"]');
          if (maxSize) field.maxSize = parseInt(maxSize.value, 10);
          if (maxSelect) field.maxSelect = parseInt(maxSelect.value, 10);
          if (mimeWrapper) field.mimeTypes = this.getTagValues(mimeWrapper);
          break;
        }

        case 'email':
        case 'url': {
          const onlyDomains = optionsPanel.querySelector('[data-opt="onlyDomains"]');
          const exceptDomains = optionsPanel.querySelector('[data-opt="exceptDomains"]');
          if (onlyDomains && onlyDomains.value.trim()) {
            field.onlyDomains = onlyDomains.value.split(',').map((d) => d.trim()).filter(Boolean);
          }
          if (exceptDomains && exceptDomains.value.trim()) {
            field.exceptDomains = exceptDomains.value.split(',').map((d) => d.trim()).filter(Boolean);
          }
          break;
        }

        case 'date': {
          const min = optionsPanel.querySelector('[data-opt="min"]');
          const max = optionsPanel.querySelector('[data-opt="max"]');
          if (min && min.value) field.min = min.value;
          if (max && max.value) field.max = max.value;
          break;
        }

        case 'json': {
          const maxSize = optionsPanel.querySelector('[data-opt="maxSize"]');
          if (maxSize && maxSize.value) field.maxSize = parseInt(maxSize.value, 10);
          break;
        }

        case 'editor': {
          const maxSize = optionsPanel.querySelector('[data-opt="maxSize"]');
          const convertURLs = optionsPanel.querySelector('[data-opt="convertURLs"]');
          if (maxSize && maxSize.value) field.maxSize = parseInt(maxSize.value, 10);
          if (convertURLs && convertURLs.checked) field.convertURLs = true;
          break;
        }
      }

      schema.push(field);
    });

    return schema;
  },

  // ── Handle Create ──────────────────────────────────────────

  async handleCreate() {
    const name = document.getElementById('col-name').value.trim();
    const type = document.getElementById('col-type').value;

    if (!name) {
      App.showToast('Please enter a collection name.', 'error');
      return;
    }

    const payload = { name, type };

    if (type === 'view') {
      const viewQuery = this._currentSqlEditor ? this._currentSqlEditor.getValue().trim() : '';
      if (!viewQuery) {
        App.showToast('Please enter a view query.', 'error');
        return;
      }
      payload.options = { query: viewQuery };
    } else {
      const schema = this.gatherSchemaFields();
      // Validate select fields have values
      for (const field of schema) {
        if (field.type === 'select' && (!field.values || field.values.length === 0)) {
          App.showToast('Select field "' + field.name + '" must have at least one value.', 'error');
          return;
        }
        if (field.type === 'relation' && !field.collectionId) {
          App.showToast('Relation field "' + field.name + '" must have a target collection.', 'error');
          return;
        }
      }
      payload.schema = schema;
    }

    const saveBtn = document.getElementById('drawer-save');
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Creating...';

    try {
      await PBClient.createCollection(payload);
      App.closeDrawer();
      App.showToast('Collection "' + name + '" created.');
      await App.loadSidebarCollections();
      App.navigate('collections');
    } catch (err) {
      const msg = (err && err.message) || 'Failed to create collection.';
      App.showToast(msg, 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = 'Create';
    }
  },

  // ── Edit Collection Drawer ──────────────────────────────────

  async showDetailModal(collection) {
    let col;
    try {
      col = await PBClient.getCollection(collection.id || collection.name);
    } catch {
      col = collection;
    }

    const fields = col.fields || col.schema || [];
    const isView = col.type === 'view';
    const typeBadge = isView ? 'View' : col.type === 'auth' ? 'Auth' : 'Base';

    const bodyHtml = `
      <div class="form-group">
        <label class="form-label">Name <span class="text-light">*</span></label>
        <div style="display: flex; align-items: center; gap: 0.75rem;">
          <input class="form-input" type="text" id="edit-col-name" value="${App.escapeHtml(col.name)}" style="flex: 1;">
          <span class="text-sm text-muted">Type: ${typeBadge}</span>
        </div>
      </div>
      <div id="edit-col-tabs" class="mt-4">
        <div style="display: flex; border-bottom: 2px solid #e2e8f0; margin-bottom: 1rem;">
          ${isView ? `
            <button class="btn btn-ghost col-tab-btn active" data-tab="query" style="border-radius: 0; border-bottom: 2px solid #4f46e5; margin-bottom: -2px;">Query</button>
          ` : `
            <button class="btn btn-ghost col-tab-btn active" data-tab="fields" style="border-radius: 0; border-bottom: 2px solid #4f46e5; margin-bottom: -2px;">Fields</button>
          `}
          <button class="btn btn-ghost col-tab-btn" data-tab="api-rules" style="border-radius: 0; margin-bottom: -2px;">API Rules</button>
          ${col.type === 'auth' ? '<button class="btn btn-ghost col-tab-btn" data-tab="options" style="border-radius: 0; margin-bottom: -2px;">Options</button>' : ''}
        </div>
        ${isView ? `
          <div class="col-tab-content" data-tab-content="query" id="edit-tab-content-query">
            <div class="form-group">
              <label class="form-label">Select query <span class="text-light">*</span></label>
              <div id="edit-view-query-editor"></div>
              <span class="form-help">SQL SELECT query. Must include id, created, and updated columns. Press <kbd>Ctrl+Space</kbd> for autocomplete.</span>
            </div>
          </div>
        ` : `
          <div class="col-tab-content" data-tab-content="fields" id="edit-tab-content-fields">
            <div id="edit-schema-fields-container"></div>
            <button type="button" class="btn btn-secondary w-full mt-3" id="edit-btn-add-field" style="border-style: dashed;">
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 1v10M1 6h10"/></svg>
              New field
            </button>
          </div>
        `}
        <div class="col-tab-content hidden" data-tab-content="api-rules" id="edit-tab-content-api-rules">
          <div class="form-group">
            <label class="form-label">List/Search rule</label>
            <input class="form-input" type="text" id="edit-col-rule-list" placeholder='Leave empty for public access' value="${App.escapeHtml((col.listRule != null ? col.listRule : '') || '')}">
          </div>
          <div class="form-group mt-3">
            <label class="form-label">View rule</label>
            <input class="form-input" type="text" id="edit-col-rule-view" placeholder='Leave empty for public access' value="${App.escapeHtml((col.viewRule != null ? col.viewRule : '') || '')}">
          </div>
          <div class="form-group mt-3">
            <label class="form-label">Create rule</label>
            <input class="form-input" type="text" id="edit-col-rule-create" placeholder='Leave empty for public access' value="${App.escapeHtml((col.createRule != null ? col.createRule : '') || '')}">
          </div>
          <div class="form-group mt-3">
            <label class="form-label">Update rule</label>
            <input class="form-input" type="text" id="edit-col-rule-update" placeholder='Leave empty for public access' value="${App.escapeHtml((col.updateRule != null ? col.updateRule : '') || '')}">
          </div>
          <div class="form-group mt-3">
            <label class="form-label">Delete rule</label>
            <input class="form-input" type="text" id="edit-col-rule-delete" placeholder='Leave empty for public access' value="${App.escapeHtml((col.deleteRule != null ? col.deleteRule : '') || '')}">
          </div>
        </div>
        ${col.type === 'auth' ? `
          <div class="col-tab-content hidden" data-tab-content="options" id="edit-tab-content-options">
            <p class="text-muted text-sm">Auth collection options.</p>
          </div>
        ` : ''}
      </div>
    `;

    const footerHtml = `
      <button class="btn btn-secondary" id="drawer-cancel-edit">Cancel</button>
      <button class="btn btn-primary" id="drawer-save-edit">Save changes</button>
    `;

    const headerActions = `
      <div class="drawer-menu-wrapper">
        <button class="drawer-menu-btn" id="drawer-col-menu-toggle" title="More options">
          <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor">
            <circle cx="8" cy="3" r="1.5"/>
            <circle cx="8" cy="8" r="1.5"/>
            <circle cx="8" cy="13" r="1.5"/>
          </svg>
        </button>
        <div class="drawer-dropdown hidden" id="drawer-col-dropdown">
          <button class="drawer-dropdown-item dropdown-danger" id="drawer-col-delete">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M2 4h12M5 4V3a1 1 0 011-1h4a1 1 0 011 1v1M6.5 7v5M9.5 7v5"/>
              <path d="M3 4l1 10a1 1 0 001 1h6a1 1 0 001-1l1-10"/>
            </svg>
            Delete collection
          </button>
        </div>
      </div>
    `;

    App.showDrawer('Edit collection', bodyHtml, footerHtml, { headerActions });

    // Tab switching
    this._initTabs();

    // Initialize SQL editor for view collections
    if (isView) {
      const vq = (col.options && col.options.query) || '';
      const sqlEditor = this.createSqlEditor(vq);
      document.getElementById('edit-view-query-editor').appendChild(sqlEditor.container);
      this._editSqlEditor = sqlEditor;
    }

    // Populate existing fields
    if (!isView) {
      const container = document.getElementById('edit-schema-fields-container');

      // Add system fields first
      this._addSystemFieldsDisplay(container, col.type || 'base');

      fields.forEach((f) => {
        this.addFieldRow(container, f);
      });

      document.getElementById('edit-btn-add-field').addEventListener('click', () => {
        this.showTypePicker(container, (type) => {
          this.addFieldRow(container, null, type);
        });
      });
    }

    // Bind buttons
    document.getElementById('drawer-cancel-edit').addEventListener('click', () => App.closeDrawer());
    document.getElementById('drawer-save-edit').addEventListener('click', () => {
      this.handleUpdate(col);
    });

    // Dropdown menu
    const menuToggle = document.getElementById('drawer-col-menu-toggle');
    const dropdown = document.getElementById('drawer-col-dropdown');
    menuToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      const isHidden = dropdown.classList.contains('hidden');
      dropdown.classList.toggle('hidden');
      if (isHidden) {
        const closeHandler = () => {
          dropdown.classList.add('hidden');
          document.removeEventListener('click', closeHandler);
        };
        setTimeout(() => document.addEventListener('click', closeHandler), 0);
      }
    });

    document.getElementById('drawer-col-delete').addEventListener('click', () => {
      dropdown.classList.add('hidden');
      App.closeDrawer();
      this.confirmDeleteCollection(col);
    });
  },

  // ── Handle Update ─────────────────────────────────────────

  async handleUpdate(collection) {
    const newName = document.getElementById('edit-col-name').value.trim();
    if (!newName) {
      App.showToast('Collection name cannot be empty.', 'error');
      return;
    }

    const payload = {};

    if (newName !== collection.name) {
      payload.name = newName;
    }

    if (collection.type === 'view') {
      const viewQuery = this._editSqlEditor ? this._editSqlEditor.getValue().trim() : '';
      if (!viewQuery) {
        App.showToast('View query cannot be empty.', 'error');
        return;
      }
      payload.options = { query: viewQuery };
    } else {
      const schema = this.gatherSchemaFields();
      // Validate
      for (const field of schema) {
        if (field.type === 'select' && (!field.values || field.values.length === 0)) {
          App.showToast('Select field "' + field.name + '" must have at least one value.', 'error');
          return;
        }
        if (field.type === 'relation' && !field.collectionId) {
          App.showToast('Relation field "' + field.name + '" must have a target collection.', 'error');
          return;
        }
      }
      payload.schema = schema;
    }

    const saveBtn = document.getElementById('drawer-save-edit');
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Saving...';

    try {
      console.log('[PPBase] updateCollection payload:', JSON.stringify(payload));
      await PBClient.updateCollection(collection.id || collection.name, payload);
      App.closeDrawer();
      App.showToast('Collection "' + newName + '" updated.');
      await App.loadSidebarCollections();
      // Refresh current view
      const updated = App.collections.find((c) => c.id === collection.id);
      if (updated) {
        App.currentCollection = updated;
        App.navigate('records', updated);
      } else {
        App.navigate('collections');
      }
    } catch (err) {
      const msg = (err && err.message) || 'Failed to update collection.';
      App.showToast(msg, 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save changes';
    }
  },

  // ── SQL Editor ─────────────────────────────────────────

  SQL_KEYWORDS: [
    'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'IN', 'IS', 'NULL',
    'AS', 'ON', 'JOIN', 'LEFT', 'RIGHT', 'INNER', 'OUTER', 'FULL', 'CROSS',
    'ORDER', 'BY', 'GROUP', 'HAVING', 'LIMIT', 'OFFSET', 'UNION', 'ALL',
    'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE', 'CREATE', 'DROP',
    'ALTER', 'TABLE', 'VIEW', 'INDEX', 'DISTINCT', 'COUNT', 'SUM', 'AVG',
    'MIN', 'MAX', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'BETWEEN', 'LIKE',
    'ILIKE', 'EXISTS', 'TRUE', 'FALSE', 'ASC', 'DESC', 'NULLS', 'FIRST',
    'LAST', 'COALESCE', 'CAST', 'WITH', 'RECURSIVE', 'OVER', 'PARTITION',
    'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'LATERAL', 'FILTER', 'ARRAY_AGG',
    'STRING_AGG', 'JSON_AGG', 'JSONB_AGG', 'NOW', 'CURRENT_TIMESTAMP',
    'EXTRACT', 'INTERVAL', 'DATE', 'TIMESTAMP', 'TIMESTAMPTZ',
    'INTEGER', 'TEXT', 'BOOLEAN', 'DOUBLE', 'PRECISION', 'VARCHAR', 'JSONB',
  ],

  async _loadDbTables() {
    if (this._dbTables) return this._dbTables;
    try {
      this._dbTables = await PBClient.getDatabaseTables();
    } catch {
      this._dbTables = [];
    }
    return this._dbTables;
  },

  /**
   * Create a SQL editor with syntax highlighting and autocomplete.
   * Returns { container, getValue, setValue }
   */
  createSqlEditor(initialValue = '') {
    const wrapper = document.createElement('div');
    wrapper.className = 'sql-editor';

    const highlight = document.createElement('pre');
    highlight.className = 'sql-editor-highlight';
    highlight.setAttribute('aria-hidden', 'true');

    const highlightCode = document.createElement('code');
    highlight.appendChild(highlightCode);

    const textarea = document.createElement('textarea');
    textarea.className = 'sql-editor-textarea';
    textarea.spellcheck = false;
    textarea.autocomplete = 'off';
    textarea.autocapitalize = 'off';
    textarea.placeholder = 'SELECT id, created, updated FROM posts WHERE published = true';
    textarea.value = initialValue;

    const autocomplete = document.createElement('div');
    autocomplete.className = 'sql-autocomplete';
    autocomplete.style.display = 'none';

    const lineNumbers = document.createElement('div');
    lineNumbers.className = 'sql-editor-lines';
    lineNumbers.textContent = '1';

    wrapper.appendChild(lineNumbers);
    wrapper.appendChild(highlight);
    wrapper.appendChild(textarea);
    wrapper.appendChild(autocomplete);

    // State
    let acItems = [];
    let acIndex = 0;
    let acVisible = false;

    const updateHighlight = () => {
      highlightCode.innerHTML = this._highlightSQL(textarea.value);
      // Sync scroll
      highlight.scrollTop = textarea.scrollTop;
      highlight.scrollLeft = textarea.scrollLeft;
      // Update line numbers
      const lines = textarea.value.split('\n').length;
      lineNumbers.innerHTML = Array.from({ length: lines }, (_, i) => i + 1).join('<br>');
    };

    const hideAutocomplete = () => {
      autocomplete.style.display = 'none';
      acVisible = false;
      acItems = [];
    };

    const showAutocomplete = (items, caretPos) => {
      if (!items.length) { hideAutocomplete(); return; }
      acItems = items.slice(0, 12);
      acIndex = 0;
      acVisible = true;

      autocomplete.innerHTML = acItems.map((item, i) => {
        const icon = item.kind === 'table' ? 'T' : item.kind === 'column' ? 'C' : 'K';
        const cls = `sql-ac-item${i === 0 ? ' active' : ''}`;
        const kindCls = `sql-ac-kind sql-ac-kind-${item.kind}`;
        return `<div class="${cls}" data-index="${i}">
          <span class="${kindCls}">${icon}</span>
          <span class="sql-ac-label">${App.escapeHtml(item.label)}</span>
          ${item.detail ? `<span class="sql-ac-detail">${App.escapeHtml(item.detail)}</span>` : ''}
        </div>`;
      }).join('');

      // Position near caret
      const rect = textarea.getBoundingClientRect();
      const coords = this._getCaretCoordinates(textarea);
      autocomplete.style.left = Math.min(coords.left, rect.width - 220) + 'px';
      autocomplete.style.top = (coords.top + 20) + 'px';
      autocomplete.style.display = 'block';

      // Bind clicks
      autocomplete.querySelectorAll('.sql-ac-item').forEach(el => {
        el.addEventListener('mousedown', (e) => {
          e.preventDefault();
          acIndex = parseInt(el.dataset.index);
          applyAutocomplete();
        });
      });
    };

    const updateAcSelection = () => {
      autocomplete.querySelectorAll('.sql-ac-item').forEach((el, i) => {
        el.classList.toggle('active', i === acIndex);
      });
      // Scroll active into view
      const active = autocomplete.querySelector('.active');
      if (active) active.scrollIntoView({ block: 'nearest' });
    };

    const applyAutocomplete = () => {
      if (!acVisible || !acItems[acIndex]) return;
      const item = acItems[acIndex];
      const pos = textarea.selectionStart;
      const text = textarea.value;

      // Find the current word being typed
      const before = text.substring(0, pos);
      const match = before.match(/[\w.]*$/);
      const wordStart = match ? pos - match[0].length : pos;

      // For column completions after "tablename.", keep the table prefix
      let insertText = item.label;
      if (match && match[0].includes('.')) {
        const dotPos = match[0].lastIndexOf('.');
        const prefix = match[0].substring(0, dotPos + 1);
        insertText = prefix + item.label;
      }

      textarea.value = text.substring(0, wordStart) + insertText + text.substring(pos);
      const newPos = wordStart + insertText.length;
      textarea.selectionStart = textarea.selectionEnd = newPos;
      hideAutocomplete();
      updateHighlight();
      textarea.focus();
    };

    const getCompletions = async (text, pos) => {
      const before = text.substring(0, pos);
      const match = before.match(/[\w.]*$/);
      if (!match || !match[0]) return [];

      const word = match[0];
      const tables = await this._loadDbTables();
      const items = [];

      // Parse table aliases from the full SQL text
      // Matches: FROM table AS alias, FROM table alias, JOIN table AS alias, JOIN table alias
      const aliases = {};
      const aliasRegex = /(?:FROM|JOIN)\s+(\w+)(?:\s+AS\s+(\w+)|\s+(\w+)(?=\s*(?:ON|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|GROUP|ORDER|LIMIT|HAVING|UNION|,|\)|$)))/gi;
      let m;
      while ((m = aliasRegex.exec(text)) !== null) {
        const tableName = m[1].toLowerCase();
        const alias = (m[2] || m[3] || '').toLowerCase();
        if (alias && alias !== tableName) {
          // Don't treat SQL keywords as aliases
          const kwSet = new Set(this.SQL_KEYWORDS.map(k => k.toLowerCase()));
          if (!kwSet.has(alias)) {
            aliases[alias] = tableName;
          }
        }
      }

      // Check if typing after "name." → show columns (supports both table names and aliases)
      if (word.includes('.')) {
        const parts = word.split('.');
        const prefix = parts[0].toLowerCase();
        const colPrefix = (parts[1] || '').toLowerCase();
        // Resolve alias to real table name, or use directly
        const realName = aliases[prefix] || prefix;
        const table = tables.find(t => t.name.toLowerCase() === realName);
        if (table) {
          for (const col of table.columns) {
            if (col.name.toLowerCase().startsWith(colPrefix)) {
              items.push({ label: col.name, kind: 'column', detail: col.type });
            }
          }
        }
        return items;
      }

      const lower = word.toLowerCase();

      // Check if we're right after FROM, JOIN, INTO, TABLE, UPDATE → prioritize tables
      const beforeWord = before.substring(0, before.length - word.length).trimEnd().toUpperCase();
      const lastKw = beforeWord.split(/\s+/).pop();
      const tableContext = ['FROM', 'JOIN', 'INTO', 'TABLE', 'UPDATE'].includes(lastKw);

      // Table names
      for (const t of tables) {
        if (t.name.toLowerCase().startsWith(lower)) {
          items.push({ label: t.name, kind: 'table', detail: `${t.columns.length} cols` });
        }
      }

      // Column names from all tables (lower priority unless in SELECT context)
      if (!tableContext) {
        for (const t of tables) {
          for (const col of t.columns) {
            if (col.name.toLowerCase().startsWith(lower)) {
              // Avoid duplicates
              if (!items.find(i => i.label === col.name && i.kind === 'column')) {
                items.push({ label: col.name, kind: 'column', detail: `${t.name}.${col.type}` });
              }
            }
          }
        }
      }

      // SQL keywords
      if (!tableContext) {
        for (const kw of this.SQL_KEYWORDS) {
          if (kw.toLowerCase().startsWith(lower) && lower.length >= 1) {
            items.push({ label: kw, kind: 'keyword' });
          }
        }
      }

      // Sort: tables first in table context, otherwise kind order
      items.sort((a, b) => {
        const order = { table: tableContext ? 0 : 1, column: tableContext ? 2 : 0, keyword: 3 };
        return (order[a.kind] || 9) - (order[b.kind] || 9);
      });

      return items;
    };

    // Events
    textarea.addEventListener('input', () => {
      updateHighlight();
      // Trigger autocomplete
      const pos = textarea.selectionStart;
      getCompletions(textarea.value, pos).then(items => {
        showAutocomplete(items, pos);
      });
    });

    textarea.addEventListener('scroll', () => {
      highlight.scrollTop = textarea.scrollTop;
      highlight.scrollLeft = textarea.scrollLeft;
      lineNumbers.style.transform = `translateY(-${textarea.scrollTop}px)`;
    });

    textarea.addEventListener('keydown', (e) => {
      // Tab key → insert spaces
      if (e.key === 'Tab' && !acVisible) {
        e.preventDefault();
        const pos = textarea.selectionStart;
        textarea.value = textarea.value.substring(0, pos) + '  ' + textarea.value.substring(pos);
        textarea.selectionStart = textarea.selectionEnd = pos + 2;
        updateHighlight();
        return;
      }

      if (acVisible) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          acIndex = (acIndex + 1) % acItems.length;
          updateAcSelection();
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          acIndex = (acIndex - 1 + acItems.length) % acItems.length;
          updateAcSelection();
          return;
        }
        if (e.key === 'Enter' || e.key === 'Tab') {
          e.preventDefault();
          applyAutocomplete();
          return;
        }
        if (e.key === 'Escape') {
          e.preventDefault();
          hideAutocomplete();
          return;
        }
      }
    });

    textarea.addEventListener('blur', () => {
      setTimeout(hideAutocomplete, 150);
    });

    // Ctrl+Space → force autocomplete
    textarea.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === ' ') {
        e.preventDefault();
        const pos = textarea.selectionStart;
        getCompletions(textarea.value, pos).then(items => {
          showAutocomplete(items, pos);
        });
      }
    });

    // Initial highlight
    updateHighlight();

    // Pre-load tables
    this._loadDbTables();

    return {
      container: wrapper,
      getValue: () => textarea.value,
      setValue: (v) => { textarea.value = v; updateHighlight(); },
    };
  },

  _highlightSQL(sql) {
    if (!sql) return '\n'; // pre needs at least a newline to size correctly

    // Escape HTML first
    let escaped = sql
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // Tokenize with regex
    // Order matters: strings first, then comments, then keywords, then numbers
    const tokens = [];
    const regex = /('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|(--.*)|(\/\*[\s\S]*?\*\/)|(\b(?:SELECT|FROM|WHERE|AND|OR|NOT|IN|IS|NULL|AS|ON|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|ORDER|BY|GROUP|HAVING|LIMIT|OFFSET|UNION|ALL|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|DROP|ALTER|TABLE|VIEW|INDEX|DISTINCT|COUNT|SUM|AVG|MIN|MAX|CASE|WHEN|THEN|ELSE|END|BETWEEN|LIKE|ILIKE|EXISTS|TRUE|FALSE|ASC|DESC|NULLS|FIRST|LAST|COALESCE|CAST|WITH|RECURSIVE|OVER|PARTITION|ROW_NUMBER|RANK|DENSE_RANK|LATERAL|FILTER|ARRAY_AGG|STRING_AGG|JSON_AGG|JSONB_AGG|NOW|CURRENT_TIMESTAMP|EXTRACT|INTERVAL|DATE|TIMESTAMP|TIMESTAMPTZ|INTEGER|TEXT|BOOLEAN|DOUBLE|PRECISION|VARCHAR|JSONB)\b)|(\b\d+(?:\.\d+)?\b)|(\*)/gi;

    let result = '';
    let lastIdx = 0;

    for (const m of escaped.matchAll(regex)) {
      // Append non-matched text
      result += escaped.substring(lastIdx, m.index);

      if (m[1]) {
        result += `<span class="sql-hl-string">${m[0]}</span>`;
      } else if (m[2]) {
        result += `<span class="sql-hl-comment">${m[0]}</span>`;
      } else if (m[3]) {
        result += `<span class="sql-hl-comment">${m[0]}</span>`;
      } else if (m[4]) {
        result += `<span class="sql-hl-keyword">${m[0].toUpperCase()}</span>`;
      } else if (m[5]) {
        result += `<span class="sql-hl-number">${m[0]}</span>`;
      } else if (m[6]) {
        result += `<span class="sql-hl-star">${m[0]}</span>`;
      }

      lastIdx = m.index + m[0].length;
    }

    result += escaped.substring(lastIdx);
    return result + '\n'; // trailing newline for sizing
  },

  /**
   * Approximate caret pixel coordinates within a textarea.
   */
  _getCaretCoordinates(textarea) {
    const mirror = document.createElement('div');
    const style = getComputedStyle(textarea);
    const props = [
      'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing',
      'padding', 'paddingLeft', 'paddingTop', 'border', 'whiteSpace',
      'wordWrap', 'overflowWrap', 'tabSize',
    ];
    mirror.style.position = 'absolute';
    mirror.style.visibility = 'hidden';
    mirror.style.whiteSpace = 'pre-wrap';
    mirror.style.wordWrap = 'break-word';
    mirror.style.width = style.width;
    for (const p of props) mirror.style[p] = style[p];

    const text = textarea.value.substring(0, textarea.selectionStart);
    mirror.textContent = text;

    const span = document.createElement('span');
    span.textContent = textarea.value.substring(textarea.selectionStart) || '.';
    mirror.appendChild(span);

    document.body.appendChild(mirror);
    const left = span.offsetLeft - textarea.scrollLeft;
    const top = span.offsetTop - textarea.scrollTop;
    document.body.removeChild(mirror);

    return { left, top };
  },

  // ── Delete Collection ──────────────────────────────────────

  confirmDeleteCollection(col) {
    const bodyHtml = `
      <div class="confirm-message">
        Are you sure you want to delete the collection <strong>${App.escapeHtml(col.name)}</strong>?
        This action cannot be undone and all associated records will be permanently deleted.
      </div>
    `;

    const footerHtml = `
      <button class="btn btn-secondary" id="modal-cancel-delete">Cancel</button>
      <button class="btn btn-danger" id="modal-confirm-delete">Delete permanently</button>
    `;

    App.showModal('Delete Collection', bodyHtml, footerHtml);

    document.getElementById('modal-cancel-delete').addEventListener('click', () => App.closeModal());
    document.getElementById('modal-confirm-delete').addEventListener('click', async () => {
      const btn = document.getElementById('modal-confirm-delete');
      btn.disabled = true;
      btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Deleting...';

      try {
        await PBClient.deleteCollection(col.id || col.name);
        App.closeModal();
        App.showToast('Collection "' + col.name + '" deleted.');
        App.currentCollection = null;
        await App.loadSidebarCollections();
        App.navigate('collections');
      } catch (err) {
        const msg = (err && err.message) || 'Failed to delete collection.';
        App.showToast(msg, 'error');
        btn.disabled = false;
        btn.textContent = 'Delete permanently';
      }
    });
  },
};
