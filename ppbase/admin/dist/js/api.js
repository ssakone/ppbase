/**
 * PPBase API Client
 * Simple fetch wrapper for communicating with the PPBase backend.
 */
const PBClient = {
  baseUrl: window.location.origin,
  token: null,

  // ── Token Management ──────────────────────────────────────

  setToken(token) {
    this.token = token;
    localStorage.setItem('ppbase_token', token);
  },

  getToken() {
    return this.token || localStorage.getItem('ppbase_token');
  },

  clearToken() {
    this.token = null;
    localStorage.removeItem('ppbase_token');
  },

  // ── Core Request ──────────────────────────────────────────

  async request(method, path, body = null) {
    const headers = { 'Content-Type': 'application/json' };
    const token = this.getToken();
    if (token) {
      headers['Authorization'] = token;
    }

    const opts = { method, headers };
    if (body) {
      opts.body = JSON.stringify(body);
    }

    const res = await fetch(this.baseUrl + path, opts);

    if (res.status === 204) {
      return null;
    }

    const data = await res.json();

    if (!res.ok) {
      throw { status: res.status, ...data };
    }

    return data;
  },

  // ── Auth ──────────────────────────────────────────────────

  login(email, password) {
    return this.request('POST', '/api/admins/auth-with-password', {
      identity: email,
      password: password,
    });
  },

  // ── Collections ───────────────────────────────────────────

  listCollections(params = '') {
    return this.request('GET', '/api/collections' + (params ? '?' + params : ''));
  },

  getCollection(idOrName) {
    return this.request('GET', '/api/collections/' + encodeURIComponent(idOrName));
  },

  createCollection(data) {
    return this.request('POST', '/api/collections', data);
  },

  updateCollection(idOrName, data) {
    return this.request('PATCH', '/api/collections/' + encodeURIComponent(idOrName), data);
  },

  deleteCollection(idOrName) {
    return this.request('DELETE', '/api/collections/' + encodeURIComponent(idOrName));
  },

  // ── SQL Metadata ────────────────────────────────────────

  getDatabaseTables() {
    return this.request('GET', '/api/collections/meta/tables');
  },

  // ── Records ───────────────────────────────────────────────

  listRecords(collection, params = '') {
    return this.request(
      'GET',
      '/api/collections/' + encodeURIComponent(collection) + '/records' + (params ? '?' + params : '')
    );
  },

  getRecord(collection, id) {
    return this.request(
      'GET',
      '/api/collections/' + encodeURIComponent(collection) + '/records/' + encodeURIComponent(id)
    );
  },

  createRecord(collection, data) {
    return this.request(
      'POST',
      '/api/collections/' + encodeURIComponent(collection) + '/records',
      data
    );
  },

  updateRecord(collection, id, data) {
    return this.request(
      'PATCH',
      '/api/collections/' + encodeURIComponent(collection) + '/records/' + encodeURIComponent(id),
      data
    );
  },

  deleteRecord(collection, id) {
    return this.request(
      'DELETE',
      '/api/collections/' + encodeURIComponent(collection) + '/records/' + encodeURIComponent(id)
    );
  },

  // ── Settings ──────────────────────────────────────────────

  getSettings() {
    return this.request('GET', '/api/settings');
  },

  updateSettings(data) {
    return this.request('PATCH', '/api/settings', data);
  },
};
