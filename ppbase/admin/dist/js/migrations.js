/**
 * PPBase Admin - Migrations UI
 *
 * Handles rendering the migrations list, status summary,
 * and apply/revert/snapshot actions.
 *
 * Depends on:
 *   - PBClient (api.js)  -- API client
 *   - App      (app.js)  -- Application controller
 */
const MigrationsUI = {

  migrations: [],
  status: null,

  // ── Load & Render Migrations ──────────────────────────────────

  async loadMigrations() {
    const body = App.els.contentBody;
    body.innerHTML = '<div class="content-loading"><div class="spinner"></div></div>';

    try {
      const [migrationsResult, statusResult] = await Promise.all([
        PBClient.request('GET', '/api/migrations'),
        PBClient.request('GET', '/api/migrations/status'),
      ]);

      this.migrations = migrationsResult.items || [];
      this.status = statusResult;
      this.render();
    } catch (err) {
      body.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
              <circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>
            </svg>
          </div>
          <h3>Failed to load migrations</h3>
          <p>${App.escapeHtml((err && err.message) || 'An unexpected error occurred.')}</p>
          <button class="btn btn-secondary" id="btn-retry-migrations">Retry</button>
        </div>
      `;
      App.bindActionEvent('btn-retry-migrations', () => this.loadMigrations());
    }
  },

  // ── Render Migrations View ────────────────────────────────────

  render() {
    const body = App.els.contentBody;
    const applied = this.status ? this.status.applied : 0;
    const pending = this.status ? this.status.pending : 0;
    const total = this.status ? this.status.total : 0;
    const lastApplied = this.status ? this.status.lastApplied : null;

    // Status summary
    let statusHtml = `
      <div class="migrations-status">
        <div class="migrations-status-cards">
          <div class="migrations-status-card">
            <div class="migrations-status-number">${total}</div>
            <div class="migrations-status-label">Total</div>
          </div>
          <div class="migrations-status-card migrations-status-applied">
            <div class="migrations-status-number">${applied}</div>
            <div class="migrations-status-label">Applied</div>
          </div>
          <div class="migrations-status-card migrations-status-pending">
            <div class="migrations-status-number">${pending}</div>
            <div class="migrations-status-label">Pending</div>
          </div>
        </div>
        ${lastApplied ? `<div class="text-sm text-muted mt-2">Last applied: ${App.escapeHtml(lastApplied)}</div>` : ''}
      </div>
    `;

    // Action buttons
    let actionsHtml = `
      <div class="migrations-actions">
        <button class="btn btn-primary" id="btn-apply-migrations" ${pending === 0 ? 'disabled' : ''}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7l3 3 5-5"/></svg>
          Apply All Pending
        </button>
        <button class="btn btn-secondary" id="btn-revert-migration" ${applied === 0 ? 'disabled' : ''}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 3L4 7l6 4"/></svg>
          Revert Last
        </button>
        <button class="btn btn-secondary" id="btn-generate-snapshot">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M7 1v12M1 7h12"/></svg>
          Generate Snapshot
        </button>
      </div>
    `;

    // Migrations table
    let tableHtml = '';
    if (this.migrations.length === 0) {
      tableHtml = `
        <div class="empty-state">
          <div class="empty-state-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
            </svg>
          </div>
          <h3>No migrations yet</h3>
          <p>Generate a snapshot to create migration files from the current database state.</p>
        </div>
      `;
    } else {
      let rows = '';
      this.migrations.forEach((mig) => {
        const statusBadge = mig.status === 'applied'
          ? '<span class="badge badge-migration-applied">applied</span>'
          : '<span class="badge badge-migration-pending">pending</span>';

        const appliedAt = mig.applied ? App.formatDate(mig.applied) : '-';

        rows += `
          <tr>
            <td class="migration-file-cell">
              <span class="migration-file-name">${App.escapeHtml(mig.file)}</span>
            </td>
            <td>${statusBadge}</td>
            <td class="text-muted text-sm">${appliedAt}</td>
          </tr>
        `;
      });

      tableHtml = `
        <div class="table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th>File</th>
                <th>Status</th>
                <th>Applied At</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
          <div class="table-footer">
            <span class="text-sm text-muted">${total} migration${total !== 1 ? 's' : ''}</span>
            <span></span>
          </div>
        </div>
      `;
    }

    body.innerHTML = '<div class="content-padded">' + statusHtml + actionsHtml + tableHtml + '</div>';

    // Bind action buttons
    App.bindActionEvent('btn-apply-migrations', () => this.applyMigrations());
    App.bindActionEvent('btn-revert-migration', () => this.revertMigration());
    App.bindActionEvent('btn-generate-snapshot', () => this.generateSnapshot());
  },

  // ── Apply All Pending Migrations ──────────────────────────────

  applyMigrations() {
    const pending = this.status ? this.status.pending : 0;

    const bodyHtml = `
      <div class="confirm-message">
        Are you sure you want to apply <strong>${pending} pending migration${pending !== 1 ? 's' : ''}</strong>?
        This will modify the database schema.
      </div>
    `;

    const footerHtml = `
      <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
      <button class="btn btn-primary" id="modal-confirm-apply">Apply migrations</button>
    `;

    App.showModal('Apply Migrations', bodyHtml, footerHtml);

    document.getElementById('modal-cancel').addEventListener('click', () => App.closeModal());
    document.getElementById('modal-confirm-apply').addEventListener('click', async () => {
      const btn = document.getElementById('modal-confirm-apply');
      btn.disabled = true;
      btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Applying...';

      try {
        const result = await PBClient.request('POST', '/api/migrations/apply');
        App.closeModal();
        const count = result.count || (result.applied ? result.applied.length : 0);
        App.showToast('Applied ' + count + ' migration' + (count !== 1 ? 's' : '') + ' successfully.');
        this.loadMigrations();
      } catch (err) {
        const msg = (err && err.message) || 'Failed to apply migrations.';
        App.showToast(msg, 'error');
        btn.disabled = false;
        btn.textContent = 'Apply migrations';
      }
    });
  },

  // ── Revert Last Migration ─────────────────────────────────────

  revertMigration() {
    const bodyHtml = `
      <div class="confirm-message">
        Are you sure you want to revert the <strong>last applied migration</strong>?
        This will undo schema changes made by that migration.
      </div>
    `;

    const footerHtml = `
      <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
      <button class="btn btn-danger" id="modal-confirm-revert">Revert migration</button>
    `;

    App.showModal('Revert Migration', bodyHtml, footerHtml);

    document.getElementById('modal-cancel').addEventListener('click', () => App.closeModal());
    document.getElementById('modal-confirm-revert').addEventListener('click', async () => {
      const btn = document.getElementById('modal-confirm-revert');
      btn.disabled = true;
      btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Reverting...';

      try {
        const result = await PBClient.request('POST', '/api/migrations/revert', { count: 1 });
        App.closeModal();
        const count = result.count || (result.reverted ? result.reverted.length : 0);
        App.showToast('Reverted ' + count + ' migration' + (count !== 1 ? 's' : '') + ' successfully.');
        this.loadMigrations();
      } catch (err) {
        const msg = (err && err.message) || 'Failed to revert migration.';
        App.showToast(msg, 'error');
        btn.disabled = false;
        btn.textContent = 'Revert migration';
      }
    });
  },

  // ── Generate Snapshot ─────────────────────────────────────────

  generateSnapshot() {
    const bodyHtml = `
      <div class="confirm-message">
        Generate a <strong>snapshot migration</strong> from the current database state?
        This will create migration files for all existing collections.
      </div>
    `;

    const footerHtml = `
      <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
      <button class="btn btn-primary" id="modal-confirm-snapshot">Generate snapshot</button>
    `;

    App.showModal('Generate Snapshot', bodyHtml, footerHtml);

    document.getElementById('modal-cancel').addEventListener('click', () => App.closeModal());
    document.getElementById('modal-confirm-snapshot').addEventListener('click', async () => {
      const btn = document.getElementById('modal-confirm-snapshot');
      btn.disabled = true;
      btn.innerHTML = '<div class="spinner spinner-sm spinner-light"></div> Generating...';

      try {
        const result = await PBClient.request('POST', '/api/migrations/snapshot');
        App.closeModal();
        const count = result.count || (result.generated ? result.generated.length : 0);
        App.showToast('Generated ' + count + ' snapshot migration' + (count !== 1 ? 's' : '') + '.');
        this.loadMigrations();
      } catch (err) {
        const msg = (err && err.message) || 'Failed to generate snapshot.';
        App.showToast(msg, 'error');
        btn.disabled = false;
        btn.textContent = 'Generate snapshot';
      }
    });
  },
};
