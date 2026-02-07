/**
 * PPBase Admin - Records UI
 *
 * Handles rendering the records table for a collection,
 * plus create/edit/delete record drawers.
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
  selectedIds: new Set(),

  // ── Load & Render Records ────────────────────────────────────

  async loadAndRender(collection, page) {
    if (page === undefined) page = 1;
    const body = App.els.contentBody;
    body.innerHTML = '<div class="content-loading"><div class="spinner"></div></div>';

    this.currentPage = page;
    this.selectedIds.clear();
    App.hideSelectionBar();

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

    // Search bar
    const searchBarHtml = `
      <div class="records-search-bar">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="7" cy="7" r="5"/>
          <path d="M15 15l-3.5-3.5"/>
        </svg>
        <input type="text" class="records-search-input" id="records-search-input" placeholder='Search term or filter like created > "2022-01-01"...'>
      </div>
    `;

    if (!records || records.length === 0) {
      body.innerHTML = searchBarHtml + `
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
        App.bindActionEvent('btn-empty-new-record', () => this.showCreateDrawer(collection));
      }
      return;
    }

    // For view collections with empty schema, infer columns from the first record
    let visibleFields;
    let viewInferred = false;
    if (schema.length > 0) {
      visibleFields = schema.slice(0, 5);
    } else if (records.length > 0) {
      const metaKeys = new Set(['id', 'collectionId', 'collectionName']);
      visibleFields = Object.keys(records[0])
        .filter(k => !metaKeys.has(k))
        .slice(0, 8)
        .map(k => ({ name: k, type: k === 'created' || k === 'updated' ? 'date' : 'text' }));
      viewInferred = true;
    } else {
      visibleFields = [];
    }

    // Build header
    let headerCells = '';
    if (!isView) {
      headerCells += '<th class="th-checkbox"><input type="checkbox" class="record-checkbox" id="select-all-records"></th>';
    }
    headerCells += '<th>ID</th>';
    visibleFields.forEach((f) => {
      headerCells += '<th>' + App.escapeHtml(f.name).toUpperCase() + '</th>';
    });
    if (!viewInferred) headerCells += '<th>Created</th>';

    // Build rows
    let rows = '';
    records.forEach((record) => {
      let cells = '';
      if (!isView) {
        cells += `<td class="td-checkbox"><input type="checkbox" class="record-checkbox" data-record-id="${App.escapeHtml(record.id)}"></td>`;
      }
      cells += '<td class="cell-id">' + App.escapeHtml(this.truncateId(record.id)) + '</td>';

      visibleFields.forEach((f) => {
        const val = record[f.name];
        if (f.type === 'date') {
          cells += '<td class="text-muted text-sm">' + App.formatDate(val) + '</td>';
        } else {
          cells += '<td>' + App.escapeHtml(this.formatCellValue(val, f)) + '</td>';
        }
      });

      if (!viewInferred) cells += '<td class="text-muted text-sm">' + App.formatDate(record.created) + '</td>';

      rows += '<tr data-record-id="' + App.escapeHtml(record.id) + '">' + cells + '</tr>';
    });

    const totalItems = result.totalItems || records.length;

    let paginationHtml = '';
    if (this.totalPages > 1) {
      const startItem = (this.currentPage - 1) * this.perPage + 1;
      const endItem = Math.min(this.currentPage * this.perPage, totalItems);
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
          <span class="text-sm text-muted">Total Found: ${totalItems}</span>
          <span></span>
        </div>
      `;
    }

    body.innerHTML = searchBarHtml + `
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

    // Row click → open edit drawer
    body.querySelectorAll('tr[data-record-id]').forEach((row) => {
      row.addEventListener('click', (e) => {
        // Don't open drawer if clicking checkbox
        if (e.target.classList.contains('record-checkbox') || e.target.closest('.td-checkbox')) return;
        const recordId = row.dataset.recordId;
        if (isView) return;
        this.showEditDrawer(collection, recordId);
      });
    });

    // Checkbox selection logic
    if (!isView) {
      const selectAll = document.getElementById('select-all-records');
      const checkboxes = body.querySelectorAll('.record-checkbox[data-record-id]');

      if (selectAll) {
        selectAll.addEventListener('change', () => {
          checkboxes.forEach(cb => {
            cb.checked = selectAll.checked;
            if (selectAll.checked) {
              this.selectedIds.add(cb.dataset.recordId);
            } else {
              this.selectedIds.delete(cb.dataset.recordId);
            }
          });
          this.updateSelectionBar(collection);
        });
      }

      checkboxes.forEach(cb => {
        cb.addEventListener('change', () => {
          if (cb.checked) {
            this.selectedIds.add(cb.dataset.recordId);
          } else {
            this.selectedIds.delete(cb.dataset.recordId);
            if (selectAll) selectAll.checked = false;
          }
          this.updateSelectionBar(collection);
        });
      });
    }
  },

  // ── Selection Bar ──────────────────────────────────────────

  updateSelectionBar(collection) {
    if (this.selectedIds.size === 0) {
      App.hideSelectionBar();
      return;
    }

    App.showSelectionBar(
      this.selectedIds.size,
      () => {
        // Reset selection
        this.selectedIds.clear();
        App.hideSelectionBar();
        const selectAll = document.getElementById('select-all-records');
        if (selectAll) selectAll.checked = false;
        document.querySelectorAll('.record-checkbox[data-record-id]').forEach(cb => {
          cb.checked = false;
        });
      },
      () => {
        // Delete selected
        this.confirmDeleteSelected(collection);
      }
    );
  },

  confirmDeleteSelected(collection) {
    const count = this.selectedIds.size;
    const bodyHtml = `
      <div class="confirm-message">
        Are you sure you want to delete <strong>${count} record${count !== 1 ? 's' : ''}</strong>?
        This action cannot be undone.
      </div>
    `;

    const footerHtml = `
      <button class="btn btn-secondary" id="modal-cancel-delete">Cancel</button>
      <button class="btn btn-danger" id="modal-confirm-delete">Delete ${count} record${count !== 1 ? 's' : ''}</button>
    `;

    App.showModal('Delete Records', bodyHtml, footerHtml);

    document.getElementById('modal-cancel-delete').addEventListener('click', () => App.closeModal());
    document.getElementById('modal-confirm-delete').addEventListener('click', async () => {
      const btn = document.getElementById('modal-confirm-delete');
      btn.disabled = true;
      btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Deleting...';

      try {
        const ids = Array.from(this.selectedIds);
        await Promise.all(ids.map(id =>
          PBClient.deleteRecord(collection.name || collection.id, id)
        ));
        App.closeModal();
        App.showToast('Deleted ' + count + ' record' + (count !== 1 ? 's' : '') + '.');
        this.selectedIds.clear();
        App.hideSelectionBar();
        this.loadAndRender(collection);
      } catch (err) {
        const msg = (err && err.message) || 'Failed to delete records.';
        App.showToast(msg, 'error');
        btn.disabled = false;
        btn.textContent = 'Delete ' + count + ' record' + (count !== 1 ? 's' : '');
      }
    });
  },

  // ── Create Record Drawer ────────────────────────────────────

  showCreateDrawer(collection) {
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
      <button class="btn btn-secondary" id="drawer-cancel">Cancel</button>
      <button class="btn btn-primary" id="drawer-save">Create</button>
    `;

    App.showDrawer('New <strong>' + App.escapeHtml(collection.name) + '</strong> record', bodyHtml, footerHtml);

    document.getElementById('drawer-cancel').addEventListener('click', () => App.closeDrawer());
    document.getElementById('drawer-save').addEventListener('click', () => {
      this.handleCreateRecord(collection, schema);
    });
  },

  async handleCreateRecord(collection, schema) {
    const data = this.gatherFormData(schema);
    const btn = document.getElementById('drawer-save');
    btn.disabled = true;
    btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Creating...';

    try {
      await PBClient.createRecord(collection.name || collection.id, data);
      App.closeDrawer();
      App.showToast('Record created successfully.');
      this.loadAndRender(collection);
    } catch (err) {
      const msg = (err && err.message) || 'Failed to create record.';
      App.showToast(msg, 'error');
      btn.disabled = false;
      btn.textContent = 'Create';
    }
  },

  // ── Edit Record Drawer ──────────────────────────────────────

  async showEditDrawer(collection, recordId) {
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
      <button class="btn btn-secondary" id="drawer-cancel">Cancel</button>
      <button class="btn btn-primary" id="drawer-save">Save changes</button>
    `;

    const headerActions = `
      <div class="drawer-menu-wrapper">
        <button class="drawer-menu-btn" id="drawer-menu-toggle" title="More options">
          <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor">
            <circle cx="8" cy="3" r="1.5"/>
            <circle cx="8" cy="8" r="1.5"/>
            <circle cx="8" cy="13" r="1.5"/>
          </svg>
        </button>
        <div class="drawer-dropdown hidden" id="drawer-dropdown-menu">
          <button class="drawer-dropdown-item" id="drawer-action-copy-json">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4 2h4l4 4v6a2 2 0 01-2 2H4a2 2 0 01-2-2V4a2 2 0 012-2z"/>
              <path d="M8 2v4h4"/>
            </svg>
            Copy raw JSON
          </button>
          <button class="drawer-dropdown-item" id="drawer-action-duplicate">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <rect x="5" y="5" width="9" height="9" rx="1.5"/>
              <path d="M3 11H2.5A1.5 1.5 0 011 9.5v-7A1.5 1.5 0 012.5 1h7A1.5 1.5 0 0111 2.5V3"/>
            </svg>
            Duplicate
          </button>
          <div class="drawer-dropdown-divider"></div>
          <button class="drawer-dropdown-item dropdown-danger" id="drawer-action-delete">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M2 4h12M5 4V3a1 1 0 011-1h4a1 1 0 011 1v1M6.5 7v5M9.5 7v5"/>
              <path d="M3 4l1 10a1 1 0 001 1h6a1 1 0 001-1l1-10"/>
            </svg>
            Delete
          </button>
        </div>
      </div>
    `;

    App.showDrawer('Edit <strong>' + App.escapeHtml(collection.name) + '</strong> record', bodyHtml, footerHtml, { headerActions });

    document.getElementById('drawer-cancel').addEventListener('click', () => App.closeDrawer());
    document.getElementById('drawer-save').addEventListener('click', () => {
      this.handleUpdateRecord(collection, record.id, schema);
    });

    // Dropdown menu toggle
    const menuToggle = document.getElementById('drawer-menu-toggle');
    const dropdown = document.getElementById('drawer-dropdown-menu');
    menuToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      const isHidden = dropdown.classList.contains('hidden');
      dropdown.classList.toggle('hidden');
      if (isHidden) {
        // Close dropdown when clicking anywhere else
        const closeHandler = () => {
          dropdown.classList.add('hidden');
          document.removeEventListener('click', closeHandler);
        };
        setTimeout(() => document.addEventListener('click', closeHandler), 0);
      }
    });

    // Copy raw JSON
    document.getElementById('drawer-action-copy-json').addEventListener('click', () => {
      dropdown.classList.add('hidden');
      const json = JSON.stringify(record, null, 2);
      navigator.clipboard.writeText(json).then(() => {
        App.showToast('Raw JSON copied to clipboard.');
      }).catch(() => {
        App.showToast('Failed to copy to clipboard.', 'error');
      });
    });

    // Duplicate
    document.getElementById('drawer-action-duplicate').addEventListener('click', () => {
      dropdown.classList.add('hidden');
      App.closeDrawer();
      this.duplicateRecord(collection, record);
    });

    // Delete
    document.getElementById('drawer-action-delete').addEventListener('click', () => {
      dropdown.classList.add('hidden');
      App.closeDrawer();
      this.confirmDeleteRecord(collection, record.id);
    });
  },

  duplicateRecord(collection, sourceRecord) {
    const schema = collection.fields || collection.schema || [];
    const data = {};
    schema.forEach(field => {
      if (sourceRecord[field.name] !== undefined) {
        data[field.name] = sourceRecord[field.name];
      }
    });

    let fieldsHtml = '';
    schema.forEach((field) => {
      fieldsHtml += this.renderFieldInput(field, data[field.name]);
    });

    const bodyHtml = '<form id="record-form" class="flex-col gap-4">' + fieldsHtml + '</form>';

    const footerHtml = `
      <button class="btn btn-secondary" id="drawer-cancel">Cancel</button>
      <button class="btn btn-primary" id="drawer-save">Create</button>
    `;

    App.showDrawer('New <strong>' + App.escapeHtml(collection.name) + '</strong> record', bodyHtml, footerHtml);

    document.getElementById('drawer-cancel').addEventListener('click', () => App.closeDrawer());
    document.getElementById('drawer-save').addEventListener('click', () => {
      this.handleCreateRecord(collection, schema);
    });
  },

  async handleUpdateRecord(collection, recordId, schema) {
    const data = this.gatherFormData(schema);
    const btn = document.getElementById('drawer-save');
    btn.disabled = true;
    btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Saving...';

    try {
      await PBClient.updateRecord(collection.name || collection.id, recordId, data);
      App.closeDrawer();
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
            let selectOptions = '<option value="">- Select -</option>';
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
