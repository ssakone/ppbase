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
    'text', 'number', 'bool', 'email', 'url', 'date',
    'select', 'json', 'file', 'relation', 'editor',
  ],

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
      <div class="table-wrapper">
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

  // ── Create Collection Modal ────────────────────────────────

  showCreateModal() {
    const bodyHtml = `
      <div class="form-group">
        <label class="form-label">Collection name</label>
        <input class="form-input" type="text" id="col-name" placeholder="e.g. posts, users, comments" required>
        <span class="form-help">Unique name, lowercase, no spaces (use underscores).</span>
      </div>
      <div class="form-group">
        <label class="form-label">Type</label>
        <select class="form-select" id="col-type">
          <option value="base">Base</option>
          <option value="auth">Auth</option>
          <option value="view">View</option>
        </select>
        <span class="form-help" id="col-type-help">Standard data collection with custom fields.</span>
      </div>
      <div id="view-query-group" class="form-group hidden">
        <label class="form-label">View query</label>
        <div id="col-view-query-editor"></div>
        <span class="form-help">SQL SELECT query that defines this view. Must include id, created, and updated columns. Press <kbd>Ctrl+Space</kbd> for autocomplete.</span>
      </div>
      <div id="schema-section">
        <div class="form-group">
          <label class="form-label">Schema fields</label>
          <div id="schema-fields-container"></div>
          <button type="button" class="btn btn-secondary btn-sm mt-2" id="btn-add-field">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 1v10M1 6h10"/></svg>
            Add field
          </button>
        </div>
      </div>
    `;

    const footerHtml = `
      <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
      <button class="btn btn-primary" id="modal-save">Create collection</button>
    `;

    App.showModal('New Collection', bodyHtml, footerHtml, { wide: true });

    // Initialize SQL editor
    const sqlEditor = this.createSqlEditor();
    document.getElementById('col-view-query-editor').appendChild(sqlEditor.container);
    this._currentSqlEditor = sqlEditor;

    // Type change handler
    const typeSelect = document.getElementById('col-type');
    const typeHelp = document.getElementById('col-type-help');
    const viewQueryGroup = document.getElementById('view-query-group');
    const schemaSection = document.getElementById('schema-section');

    typeSelect.addEventListener('change', () => {
      const t = typeSelect.value;
      if (t === 'view') {
        viewQueryGroup.classList.remove('hidden');
        schemaSection.classList.add('hidden');
        typeHelp.textContent = 'Read-only collection backed by a SQL SELECT query.';
      } else {
        viewQueryGroup.classList.add('hidden');
        schemaSection.classList.remove('hidden');
        if (t === 'auth') {
          typeHelp.textContent = 'Auth collections include built-in email/password fields.';
        } else {
          typeHelp.textContent = 'Standard data collection with custom fields.';
        }
      }
    });

    // Bind add-field
    document.getElementById('btn-add-field').addEventListener('click', () => {
      this.addFieldRow(document.getElementById('schema-fields-container'));
    });

    // Add one field row by default
    this.addFieldRow(document.getElementById('schema-fields-container'));

    // Bind cancel/save
    document.getElementById('modal-cancel').addEventListener('click', () => App.closeModal());
    document.getElementById('modal-save').addEventListener('click', () => this.handleCreate());
  },

  // ── Field Row with Options ─────────────────────────────────

  addFieldRow(container, existingField) {
    const row = document.createElement('div');
    row.className = 'field-editor-row';

    const fieldType = existingField ? (existingField.type || 'text') : 'text';

    let typeOptions = '';
    this.fieldTypes.forEach((t) => {
      const selected = (t === fieldType) ? 'selected' : '';
      typeOptions += `<option value="${t}" ${selected}>${t}</option>`;
    });

    row.innerHTML = `
      <div class="field-editor-header">
        <div class="field-editor-inputs">
          <div class="form-group" style="flex: 1;">
            <input class="form-input" type="text" placeholder="Field name" data-role="field-name"
              value="${existingField ? App.escapeHtml(existingField.name || '') : ''}">
          </div>
          <div class="form-group" style="flex: 0 0 160px;">
            <select class="form-select" data-role="field-type">${typeOptions}</select>
          </div>
          <label class="form-checkbox field-required-toggle" title="Required">
            <input type="checkbox" data-role="field-required" ${existingField && existingField.required ? 'checked' : ''}>
            <span class="text-sm">Required</span>
          </label>
          <button type="button" class="btn btn-ghost btn-icon btn-remove-field" title="Remove field">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M3 3l8 8M11 3l-8 8"/></svg>
          </button>
        </div>
        <div class="field-options-panel" data-role="field-options"></div>
      </div>
    `;

    // Type change → show relevant options
    const typeSelect = row.querySelector('[data-role="field-type"]');
    const optionsPanel = row.querySelector('[data-role="field-options"]');

    typeSelect.addEventListener('change', () => {
      this.renderFieldOptions(typeSelect.value, optionsPanel);
    });

    // Render options for current type
    this.renderFieldOptions(fieldType, optionsPanel);

    // Pre-fill existing field options
    if (existingField) {
      this._populateFieldOptions(optionsPanel, existingField);
    }

    row.querySelector('.btn-remove-field').addEventListener('click', () => row.remove());
    container.appendChild(row);

    if (!existingField) {
      row.querySelector('[data-role="field-name"]').focus();
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
    const rows = document.querySelectorAll('.field-editor-row');
    const schema = [];

    rows.forEach((row) => {
      const name = row.querySelector('[data-role="field-name"]').value.trim();
      const type = row.querySelector('[data-role="field-type"]').value;
      const required = row.querySelector('[data-role="field-required"]').checked;

      if (!name) return;

      // Build field object (flat PocketBase v0.23+ format)
      const field = { name, type, required };

      // Gather type-specific options
      const optionsPanel = row.querySelector('[data-role="field-options"]');

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

    const saveBtn = document.getElementById('modal-save');
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Creating...';

    try {
      await PBClient.createCollection(payload);
      App.closeModal();
      App.showToast('Collection "' + name + '" created.');
      await App.loadSidebarCollections();
      App.navigate('collections');
    } catch (err) {
      const msg = (err && err.message) || 'Failed to create collection.';
      App.showToast(msg, 'error');
      saveBtn.disabled = false;
      saveBtn.textContent = 'Create collection';
    }
  },

  // ── Edit Collection Modal ──────────────────────────────────

  async showDetailModal(collection) {
    let col;
    try {
      col = await PBClient.getCollection(collection.id || collection.name);
    } catch {
      col = collection;
    }

    const fields = col.fields || col.schema || [];
    const isView = col.type === 'view';

    let viewQueryHtml = '';
    if (isView) {
      viewQueryHtml = `
        <div class="form-group" id="edit-view-query-group">
          <label class="form-label">View query</label>
          <div id="edit-view-query-editor"></div>
          <span class="form-help">SQL SELECT query. Must include id, created, and updated columns. Press <kbd>Ctrl+Space</kbd> for autocomplete.</span>
        </div>
      `;
    }

    const bodyHtml = `
      <div class="form-group">
        <label class="form-label">Collection name</label>
        <input class="form-input" type="text" id="edit-col-name" value="${App.escapeHtml(col.name)}">
      </div>
      <dl class="info-grid mb-4" style="margin-top: 0.5rem;">
        <dt>Type</dt>
        <dd><span class="badge ${isView ? 'badge-yellow' : col.type === 'auth' ? 'badge-green' : 'badge-blue'}">${App.escapeHtml(col.type)}</span></dd>
        <dt>ID</dt>
        <dd class="text-sm text-muted" style="font-family: monospace;">${App.escapeHtml(col.id || '-')}</dd>
      </dl>
      ${viewQueryHtml}
      ${!isView ? `
        <div>
          <label class="form-label">Schema fields</label>
          <div id="edit-schema-fields-container"></div>
          <button type="button" class="btn btn-secondary btn-sm mt-2" id="edit-btn-add-field">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 1v10M1 6h10"/></svg>
            Add field
          </button>
        </div>
      ` : ''}
    `;

    const footerHtml = `
      <button class="btn btn-danger btn-sm" id="modal-delete-collection">Delete</button>
      <div class="flex-1"></div>
      <button class="btn btn-secondary" id="modal-cancel-edit">Cancel</button>
      <button class="btn btn-primary" id="modal-save-edit">Save changes</button>
    `;

    App.showModal('Edit: ' + col.name, bodyHtml, footerHtml, { wide: true });

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
      fields.forEach((f) => {
        this.addFieldRow(container, f);
      });

      document.getElementById('edit-btn-add-field').addEventListener('click', () => {
        this.addFieldRow(container);
      });
    }

    // Bind buttons
    document.getElementById('modal-cancel-edit').addEventListener('click', () => App.closeModal());
    document.getElementById('modal-delete-collection').addEventListener('click', () => {
      this.confirmDeleteCollection(col);
    });
    document.getElementById('modal-save-edit').addEventListener('click', () => {
      this.handleUpdate(col);
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

    const saveBtn = document.getElementById('modal-save-edit');
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Saving...';

    try {
      await PBClient.updateCollection(collection.id || collection.name, payload);
      App.closeModal();
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

      // Check if typing after "tablename." → show columns
      if (word.includes('.')) {
        const parts = word.split('.');
        const tableName = parts[0].toLowerCase();
        const colPrefix = (parts[1] || '').toLowerCase();
        const table = tables.find(t => t.name.toLowerCase() === tableName);
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
