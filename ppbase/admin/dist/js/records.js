/**
 * PPBase Admin - Records UI
 *
 * Handles rendering the records table for a collection,
 * plus create/edit/delete record modals.
 * Supports all field types with proper input controls.
 *
 * Depends on:
 *   - PBClient (api.js)
 *   - App      (app.js)
 */
const RecordsUI = {

  currentPage: 1,
  totalPages: 1,
  perPage: 30,
  currentRecords: [],

  // ── Load & Render Records ────────────────────────────────────

  async loadAndRender(collection, page) {
    if (page === undefined) page = 1;
    const body = App.els.contentBody;
    body.innerHTML = '<div class="content-loading"><div class="spinner"></div></div>';

    this.currentPage = page;

    try {
      const params = 'page=' + page + '&perPage=' + this.perPage;
      const result = await PBClient.listRecords(collection.name || collection.id, params);

      this.currentRecords = result.items || [];
      this.totalPages = result.totalPages || 1;
      this.currentPage = result.page || page;

      this.renderTable(collection, this.currentRecords, result);
    } catch (err) {
      body.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
              <circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>
            </svg>
          </div>
          <h3>Failed to load records</h3>
          <p>${App.escapeHtml((err && err.message) || 'An unexpected error occurred.')}</p>
          <button class="btn btn-secondary" id="btn-retry-records">Retry</button>
        </div>
      `;
      App.bindActionEvent('btn-retry-records', () => this.loadAndRender(collection, page));
    }
  },

  // ── Render Records Table ─────────────────────────────────────

  renderTable(collection, records, result) {
    const body = App.els.contentBody;
    const schema = collection.fields || collection.schema || [];
    const isView = collection.type === 'view';

    if (!records || records.length === 0) {
      body.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="12" y1="18" x2="12" y2="12"/>
              <line x1="9" y1="15" x2="15" y2="15"/>
            </svg>
          </div>
          <h3>No records yet</h3>
          <p>${isView ? 'This view has no matching records.' : 'Add your first record to the "' + App.escapeHtml(collection.name) + '" collection.'}</p>
          ${isView ? '' : `
            <button class="btn btn-primary" id="btn-empty-new-record">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M7 1v12M1 7h12"/></svg>
              New record
            </button>
          `}
        </div>
      `;
      if (!isView) {
        App.bindActionEvent('btn-empty-new-record', () => this.showCreateModal(collection));
      }
      return;
    }

    // For view collections with empty schema, infer columns from the first record
    let visibleFields;
    let viewInferred = false;
    if (schema.length > 0) {
      visibleFields = schema.slice(0, 5);
    } else if (records.length > 0) {
      // Only exclude PPBase metadata keys — keep created/updated since the user chose them
      const metaKeys = new Set(['id', 'collectionId', 'collectionName']);
      visibleFields = Object.keys(records[0])
        .filter(k => !metaKeys.has(k))
        .slice(0, 8)
        .map(k => ({ name: k, type: k === 'created' || k === 'updated' ? 'date' : 'text' }));
      viewInferred = true;
    } else {
      visibleFields = [];
    }

    let headerCells = '<th>ID</th>';
    visibleFields.forEach((f) => {
      headerCells += '<th>' + App.escapeHtml(f.name).toUpperCase() + '</th>';
    });
    // Only add the hardcoded Created column for base/auth collections
    if (!viewInferred) headerCells += '<th>Created</th>';
    if (!isView) headerCells += '<th></th>';

    let rows = '';
    records.forEach((record) => {
      let cells = '<td class="cell-id">' + App.escapeHtml(this.truncateId(record.id)) + '</td>';

      visibleFields.forEach((f) => {
        const val = record[f.name];
        if (f.type === 'date') {
          cells += '<td class="text-muted text-sm">' + App.formatDate(val) + '</td>';
        } else {
          cells += '<td>' + App.escapeHtml(this.formatCellValue(val, f)) + '</td>';
        }
      });

      if (!viewInferred) cells += '<td class="text-muted text-sm">' + App.formatDate(record.created) + '</td>';

      if (!isView) {
        cells += `
          <td class="row-actions">
            <button class="btn btn-ghost btn-sm btn-edit-record" data-id="${App.escapeHtml(record.id)}" title="Edit">
              <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M10 1.5l2.5 2.5L4.5 12H2v-2.5L10 1.5z"/>
              </svg>
            </button>
            <button class="btn btn-ghost btn-sm btn-delete-record" data-id="${App.escapeHtml(record.id)}" title="Delete">
              <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
                <path d="M2.5 4h9M5 4V2.5a.5.5 0 01.5-.5h3a.5.5 0 01.5.5V4M11 4v7.5a1 1 0 01-1 1H4a1 1 0 01-1-1V4"/>
              </svg>
            </button>
          </td>
        `;
      }

      rows += '<tr>' + cells + '</tr>';
    });

    const totalItems = result.totalItems || records.length;
    const startItem = (this.currentPage - 1) * this.perPage + 1;
    const endItem = Math.min(this.currentPage * this.perPage, totalItems);

    let paginationHtml = '';
    if (this.totalPages > 1) {
      paginationHtml = `
        <div class="table-footer">
          <span class="text-sm text-muted">${startItem}\u2013${endItem} of ${totalItems} records</span>
          <div class="pagination">
            <button class="pagination-btn" id="page-prev" ${this.currentPage <= 1 ? 'disabled' : ''}>Previous</button>
            <button class="pagination-btn" id="page-next" ${this.currentPage >= this.totalPages ? 'disabled' : ''}>Next</button>
          </div>
        </div>
      `;
    } else {
      paginationHtml = `
        <div class="table-footer">
          <span class="text-sm text-muted">${totalItems} record${totalItems !== 1 ? 's' : ''}</span>
          <span></span>
        </div>
      `;
    }

    body.innerHTML = `
      <div class="table-wrapper">
        <table class="data-table">
          <thead><tr>${headerCells}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
        ${paginationHtml}
      </div>
    `;

    if (this.totalPages > 1) {
      const prevBtn = document.getElementById('page-prev');
      const nextBtn = document.getElementById('page-next');
      if (prevBtn) prevBtn.addEventListener('click', () => this.loadAndRender(collection, this.currentPage - 1));
      if (nextBtn) nextBtn.addEventListener('click', () => this.loadAndRender(collection, this.currentPage + 1));
    }

    if (!isView) {
      body.querySelectorAll('.btn-edit-record').forEach((btn) => {
        btn.addEventListener('click', () => this.showEditModal(collection, btn.dataset.id));
      });
      body.querySelectorAll('.btn-delete-record').forEach((btn) => {
        btn.addEventListener('click', () => this.confirmDeleteRecord(collection, btn.dataset.id));
      });
    }
  },

  // ── Create Record Modal ──────────────────────────────────────

  showCreateModal(collection) {
    const schema = collection.fields || collection.schema || [];

    if (schema.length === 0) {
      App.showToast('This collection has no schema fields defined.', 'error');
      return;
    }

    let fieldsHtml = '';
    schema.forEach((field) => {
      fieldsHtml += this.renderFieldInput(field);
    });

    const bodyHtml = '<form id="record-form" class="flex-col gap-4">' + fieldsHtml + '</form>';

    const footerHtml = `
      <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
      <button class="btn btn-primary" id="modal-save">Create record</button>
    `;

    App.showModal('New Record', bodyHtml, footerHtml);

    document.getElementById('modal-cancel').addEventListener('click', () => App.closeModal());
    document.getElementById('modal-save').addEventListener('click', () => {
      this.handleCreateRecord(collection, schema);
    });
  },

  async handleCreateRecord(collection, schema) {
    const data = this.gatherFormData(schema);
    const btn = document.getElementById('modal-save');
    btn.disabled = true;
    btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Creating...';

    try {
      await PBClient.createRecord(collection.name || collection.id, data);
      App.closeModal();
      App.showToast('Record created successfully.');
      this.loadAndRender(collection);
    } catch (err) {
      const msg = (err && err.message) || 'Failed to create record.';
      App.showToast(msg, 'error');
      btn.disabled = false;
      btn.textContent = 'Create record';
    }
  },

  // ── Edit Record Modal ──────────────────────────────────────

  async showEditModal(collection, recordId) {
    let record;
    try {
      record = await PBClient.getRecord(collection.name || collection.id, recordId);
    } catch {
      App.showToast('Failed to load record.', 'error');
      return;
    }

    const schema = collection.fields || collection.schema || [];
    let fieldsHtml = '';
    schema.forEach((field) => {
      fieldsHtml += this.renderFieldInput(field, record[field.name]);
    });

    const bodyHtml = `
      <div class="text-sm text-muted mb-4" style="font-family: monospace;">ID: ${App.escapeHtml(record.id)}</div>
      <form id="record-form" class="flex-col gap-4">${fieldsHtml}</form>
    `;

    const footerHtml = `
      <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
      <button class="btn btn-primary" id="modal-save">Save changes</button>
    `;

    App.showModal('Edit Record', bodyHtml, footerHtml);

    document.getElementById('modal-cancel').addEventListener('click', () => App.closeModal());
    document.getElementById('modal-save').addEventListener('click', () => {
      this.handleUpdateRecord(collection, record.id, schema);
    });
  },

  async handleUpdateRecord(collection, recordId, schema) {
    const data = this.gatherFormData(schema);
    const btn = document.getElementById('modal-save');
    btn.disabled = true;
    btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Saving...';

    try {
      await PBClient.updateRecord(collection.name || collection.id, recordId, data);
      App.closeModal();
      App.showToast('Record updated successfully.');
      this.loadAndRender(collection);
    } catch (err) {
      const msg = (err && err.message) || 'Failed to update record.';
      App.showToast(msg, 'error');
      btn.disabled = false;
      btn.textContent = 'Save changes';
    }
  },

  // ── Delete Record ──────────────────────────────────────────

  confirmDeleteRecord(collection, recordId) {
    const shortId = this.truncateId(recordId);

    const bodyHtml = `
      <div class="confirm-message">
        Are you sure you want to delete record <strong>${App.escapeHtml(shortId)}</strong>?
        This action cannot be undone.
      </div>
    `;

    const footerHtml = `
      <button class="btn btn-secondary" id="modal-cancel-delete">Cancel</button>
      <button class="btn btn-danger" id="modal-confirm-delete">Delete record</button>
    `;

    App.showModal('Delete Record', bodyHtml, footerHtml);

    document.getElementById('modal-cancel-delete').addEventListener('click', () => App.closeModal());
    document.getElementById('modal-confirm-delete').addEventListener('click', async () => {
      const btn = document.getElementById('modal-confirm-delete');
      btn.disabled = true;
      btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Deleting...';

      try {
        await PBClient.deleteRecord(collection.name || collection.id, recordId);
        App.closeModal();
        App.showToast('Record deleted.');
        this.loadAndRender(collection);
      } catch (err) {
        const msg = (err && err.message) || 'Failed to delete record.';
        App.showToast(msg, 'error');
        btn.disabled = false;
        btn.textContent = 'Delete record';
      }
    });
  },

  // ── Form Field Rendering ───────────────────────────────────

  /**
   * Render an HTML form input for a single schema field.
   * Uses proper controls: select dropdown for select fields,
   * collection-aware input for relations, etc.
   */
  renderFieldInput(field, value) {
    const name = App.escapeHtml(field.name);
    const type = field.type;
    const opts = field.options || {};
    const escapedValue = (value !== undefined && value !== null) ? App.escapeHtml(String(value)) : '';
    const requiredAttr = field.required ? 'required' : '';
    const requiredLabel = field.required ? ' <span class="text-light">*</span>' : '';

    switch (type) {
      case 'bool': {
        const checked = value ? 'checked' : '';
        return `
          <div class="form-group">
            <label class="form-checkbox">
              <input type="checkbox" data-field="${name}" data-type="${type}" ${checked}>
              ${name}${requiredLabel}
            </label>
          </div>
        `;
      }

      case 'number':
        return `
          <div class="form-group">
            <label class="form-label">${name}${requiredLabel}</label>
            <input class="form-input" type="number" step="${(field.onlyInt || opts.onlyInt) ? '1' : 'any'}" data-field="${name}" data-type="${type}" value="${escapedValue}" ${requiredAttr}
              ${(field.min != null || opts.min != null) ? 'min="' + (field.min != null ? field.min : opts.min) + '"' : ''}
              ${(field.max != null || opts.max != null) ? 'max="' + (field.max != null ? field.max : opts.max) + '"' : ''}>
          </div>
        `;

      case 'email':
        return `
          <div class="form-group">
            <label class="form-label">${name}${requiredLabel}</label>
            <input class="form-input" type="email" data-field="${name}" data-type="${type}" value="${escapedValue}" placeholder="user@example.com" ${requiredAttr}>
          </div>
        `;

      case 'url':
        return `
          <div class="form-group">
            <label class="form-label">${name}${requiredLabel}</label>
            <input class="form-input" type="url" data-field="${name}" data-type="${type}" value="${escapedValue}" placeholder="https://..." ${requiredAttr}>
          </div>
        `;

      case 'date':
        return `
          <div class="form-group">
            <label class="form-label">${name}${requiredLabel}</label>
            <input class="form-input" type="datetime-local" data-field="${name}" data-type="${type}" value="${escapedValue}" ${requiredAttr}>
          </div>
        `;

      case 'json': {
        const jsonVal = value ? JSON.stringify(value, null, 2) : '';
        return `
          <div class="form-group">
            <label class="form-label">${name}${requiredLabel}</label>
            <textarea class="form-textarea" data-field="${name}" data-type="${type}" rows="4" placeholder="{}" ${requiredAttr}>${App.escapeHtml(jsonVal)}</textarea>
          </div>
        `;
      }

      case 'editor':
        return `
          <div class="form-group">
            <label class="form-label">${name}${requiredLabel}</label>
            <textarea class="form-textarea" data-field="${name}" data-type="${type}" rows="6" ${requiredAttr}>${escapedValue}</textarea>
          </div>
        `;

      case 'select': {
        const values = field.values || opts.values || [];
        const maxSelect = field.maxSelect || opts.maxSelect || 1;

        if (values.length > 0) {
          if (maxSelect <= 1) {
            // Single select → dropdown
            let selectOptions = '<option value="">-- Select --</option>';
            values.forEach((v) => {
              const selected = (String(value) === v) ? 'selected' : '';
              selectOptions += `<option value="${App.escapeHtml(v)}" ${selected}>${App.escapeHtml(v)}</option>`;
            });
            return `
              <div class="form-group">
                <label class="form-label">${name}${requiredLabel}</label>
                <select class="form-select" data-field="${name}" data-type="${type}" ${requiredAttr}>
                  ${selectOptions}
                </select>
              </div>
            `;
          } else {
            // Multi-select → checkboxes
            const currentValues = Array.isArray(value) ? value : (value ? [value] : []);
            let checkboxes = '';
            values.forEach((v) => {
              const checked = currentValues.includes(v) ? 'checked' : '';
              checkboxes += `
                <label class="form-checkbox">
                  <input type="checkbox" data-multi-select="${name}" value="${App.escapeHtml(v)}" ${checked}>
                  <span>${App.escapeHtml(v)}</span>
                </label>
              `;
            });
            return `
              <div class="form-group">
                <label class="form-label">${name}${requiredLabel} <span class="badge badge-purple" style="vertical-align: middle;">multi</span></label>
                <div class="multi-select-options" data-field="${name}" data-type="${type}">
                  ${checkboxes}
                </div>
              </div>
            `;
          }
        }

        // Fallback: no values defined
        return `
          <div class="form-group">
            <label class="form-label">${name}${requiredLabel} <span class="badge badge-purple" style="vertical-align: middle;">select</span></label>
            <input class="form-input" type="text" data-field="${name}" data-type="${type}" value="${escapedValue}" placeholder="Enter a value" ${requiredAttr}>
            <span class="form-help">No predefined values. Enter a value manually.</span>
          </div>
        `;
      }

      case 'relation': {
        const collectionId = field.collectionId || opts.collectionId || '';
        const relCol = collectionId ? (App.collections || []).find((c) => c.id === collectionId) : null;
        const colLabel = relCol ? relCol.name : (collectionId || 'unknown');
        const maxSelect = field.maxSelect || opts.maxSelect || 1;

        if (maxSelect > 1) {
          // Multi-relation: show as textarea for IDs
          const currentValues = Array.isArray(value) ? value.join(', ') : (value || '');
          return `
            <div class="form-group">
              <label class="form-label">${name}${requiredLabel} <span class="badge badge-red" style="vertical-align: middle;">relation</span></label>
              <textarea class="form-textarea" data-field="${name}" data-type="${type}" data-multi="true" rows="2" placeholder="Record IDs, comma-separated" ${requiredAttr}>${App.escapeHtml(currentValues)}</textarea>
              <span class="form-help">Links to <strong>${App.escapeHtml(colLabel)}</strong> collection. Enter record IDs separated by commas.</span>
            </div>
          `;
        }

        return `
          <div class="form-group">
            <label class="form-label">${name}${requiredLabel} <span class="badge badge-red" style="vertical-align: middle;">relation</span></label>
            <input class="form-input" type="text" data-field="${name}" data-type="${type}" value="${escapedValue}" placeholder="Record ID" ${requiredAttr}>
            <span class="form-help">Links to <strong>${App.escapeHtml(colLabel)}</strong> collection. Enter the related record ID.</span>
          </div>
        `;
      }

      case 'file':
        return `
          <div class="form-group">
            <label class="form-label">${name}${requiredLabel} <span class="badge badge-yellow" style="vertical-align: middle;">file</span></label>
            <input class="form-input" type="text" data-field="${name}" data-type="${type}" value="${escapedValue}" placeholder="Filename" ${requiredAttr} disabled>
            <span class="form-help">File uploads are not yet supported in the admin UI.</span>
          </div>
        `;

      default: // text
        return `
          <div class="form-group">
            <label class="form-label">${name}${requiredLabel}</label>
            <input class="form-input" type="text" data-field="${name}" data-type="${type}" value="${escapedValue}" ${requiredAttr}>
          </div>
        `;
    }
  },

  // ── Gather Form Data ───────────────────────────────────────

  gatherFormData(schema) {
    const data = {};

    schema.forEach((field) => {
      const type = field.type;

      // Handle multi-select (checkboxes)
      if (type === 'select') {
        const opts = field.options || {};
        const maxSelect = field.maxSelect || opts.maxSelect || 1;
        if (maxSelect > 1) {
          const checkboxes = document.querySelectorAll('[data-multi-select="' + field.name + '"]');
          if (checkboxes.length > 0) {
            data[field.name] = Array.from(checkboxes)
              .filter((cb) => cb.checked)
              .map((cb) => cb.value);
            return;
          }
        }
      }

      // Handle multi-relation (textarea with comma-separated IDs)
      if (type === 'relation') {
        const el = document.querySelector('[data-field="' + field.name + '"][data-multi="true"]');
        if (el) {
          const ids = el.value.split(',').map((s) => s.trim()).filter(Boolean);
          data[field.name] = ids;
          return;
        }
      }

      const el = document.querySelector('[data-field="' + field.name + '"]');
      if (!el) return;

      switch (type) {
        case 'bool':
          data[field.name] = el.checked;
          break;

        case 'number': {
          const numVal = el.value.trim();
          data[field.name] = numVal !== '' ? Number(numVal) : null;
          break;
        }

        case 'json':
          try {
            const jsonStr = el.value.trim();
            data[field.name] = jsonStr ? JSON.parse(jsonStr) : null;
          } catch {
            data[field.name] = el.value.trim();
          }
          break;

        case 'file':
          break;

        default:
          data[field.name] = el.value;
          break;
      }
    });

    return data;
  },

  // ── Helpers ────────────────────────────────────────────────

  truncateId(id) {
    if (!id) return '-';
    if (id.length <= 12) return id;
    return id.substring(0, 5) + '...' + id.substring(id.length - 4);
  },

  formatCellValue(value, field) {
    if (value === null || value === undefined) return '-';
    const type = typeof field === 'string' ? field : field.type;

    switch (type) {
      case 'bool':
        return value ? 'Yes' : 'No';

      case 'json':
        if (typeof value === 'object') {
          const str = JSON.stringify(value);
          return str.length > 50 ? str.substring(0, 50) + '...' : str;
        }
        return String(value);

      case 'select': {
        if (Array.isArray(value)) {
          return value.join(', ');
        }
        return String(value);
      }

      case 'relation':
        if (Array.isArray(value)) {
          return value.length + ' relation' + (value.length !== 1 ? 's' : '');
        }
        return this.truncateId(String(value));

      case 'file':
        if (Array.isArray(value)) {
          return value.length + ' file' + (value.length !== 1 ? 's' : '');
        }
        return String(value);

      default: {
        const str = String(value);
        return str.length > 60 ? str.substring(0, 60) + '...' : str;
      }
    }
  },
};
