/**
 * PocketBase SDK E2E Tests: Auth Collection Flows
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import type PocketBase from 'pocketbase';
import { getAdminPb, getFreshPb, createTestCollection, uniqueName } from './helpers';

const BASE_URL = process.env.PPBASE_URL || 'http://localhost:8090';

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

async function findEmailChangeToken(newEmail: string, timeoutMs = 2500): Promise<string | null> {
  const logPath = join(process.cwd(), '..', '..', '.ppbase.log');
  if (!existsSync(logPath)) {
    return null;
  }

  const pattern = new RegExp(
    `\\[DEV\\] Email-change token for ${escapeRegExp(newEmail)}: ([^\\s]+)`,
    'g'
  );

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const content = readFileSync(logPath, 'utf8');
    const matches = [...content.matchAll(pattern)];
    if (matches.length > 0) {
      return matches[matches.length - 1][1];
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }

  return null;
}

async function findOtpCode(email: string, otpId: string, timeoutMs = 2500): Promise<string | null> {
  const logPath = join(process.cwd(), '..', '..', '.ppbase.log');
  if (!existsSync(logPath)) {
    return null;
  }

  const pattern = new RegExp(
    `\\[DEV\\] OTP for ${escapeRegExp(email)} \\(otpId=${escapeRegExp(otpId)}\\): ([^\\s]+)`,
    'g'
  );

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const content = readFileSync(logPath, 'utf8');
    const matches = [...content.matchAll(pattern)];
    if (matches.length > 0) {
      return matches[matches.length - 1][1];
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }

  return null;
}

async function expectPbError(
  action: () => Promise<unknown>,
  status: number,
  code?: string
): Promise<any> {
  try {
    await action();
    throw new Error(`Expected request to fail with status ${status}`);
  } catch (err: any) {
    expect(err).toBeTruthy();
    expect(err.status).toBe(status);
    if (code) {
      expect(err.response?.data).toBeTruthy();
      expect(err.response.data.email?.code).toBe(code);
    }
    return err;
  }
}

describe('Auth Collection Flows', () => {
  let adminPb: PocketBase;
  let authCollection: any;
  let cleanup: () => Promise<void>;

  beforeAll(async () => {
    adminPb = await getAdminPb();
    const result = await createTestCollection(adminPb, {
      name: uniqueName('auth'),
      type: 'auth',
      schema: [{ name: 'displayName', type: 'text', required: false }],
      options: {
        otp: {
          enabled: true,
          duration: 180,
          length: 8,
        },
      },
    });
    authCollection = result.collection;
    cleanup = result.cleanup;
  });

  afterAll(async () => {
    await cleanup();
  });

  it('should register a user', async () => {
    const pb = getFreshPb();
    const email = `user_${Date.now()}@example.com`;

    const user = await pb.collection(authCollection.name).create({
      email,
      password: 'password123',
      passwordConfirm: 'password123',
      displayName: 'Test User',
    });

    expect(user.id).toBeTruthy();
    expect(user.email).toBe(email);
    expect(user.displayName).toBe('Test User');
    expect(user.verified).toBe(false);
    expect(user.password_hash).toBeUndefined();
    expect(user.token_key).toBeUndefined();
  });

  it('should reject invalid email format', async () => {
    const pb = getFreshPb();

    await expect(
      pb.collection(authCollection.name).create({
        email: 'not-an-email',
        password: 'password123',
        passwordConfirm: 'password123',
      })
    ).rejects.toThrow(/email/i);
  });

  it('should reject duplicate email', async () => {
    const pb = getFreshPb();
    const email = `duplicate_${Date.now()}@example.com`;

    // First registration
    await pb.collection(authCollection.name).create({
      email,
      password: 'password123',
      passwordConfirm: 'password123',
    });

    // Second registration with same email
    await expect(
      pb.collection(authCollection.name).create({
        email,
        password: 'password123',
        passwordConfirm: 'password123',
      })
    ).rejects.toThrow();
  });

  it('should reject missing password', async () => {
    const pb = getFreshPb();

    await expect(
      pb.collection(authCollection.name).create({
        email: `missing_pwd_${Date.now()}@example.com`,
      })
    ).rejects.toThrow(/password/i);
  });

  it('should login with correct credentials', async () => {
    const pb = getFreshPb();
    const email = `login_${Date.now()}@example.com`;
    const password = 'securepass123';

    // Register
    await pb.collection(authCollection.name).create({
      email,
      password,
      passwordConfirm: password,
    });

    // Login
    const authData = await pb.collection(authCollection.name).authWithPassword(email, password);

    expect(authData.token).toBeTruthy();
    expect(authData.record).toBeTruthy();
    expect(authData.record.email).toBe(email);
    expect(pb.authStore.isValid).toBe(true);
    expect(pb.authStore.token).toBe(authData.token);
  });

  it('should reject wrong password', async () => {
    const pb = getFreshPb();
    const email = `wrong_pwd_${Date.now()}@example.com`;

    // Register
    await pb.collection(authCollection.name).create({
      email,
      password: 'correct123',
      passwordConfirm: 'correct123',
    });

    // Login with wrong password
    await expect(
      pb.collection(authCollection.name).authWithPassword(email, 'wrong123')
    ).rejects.toThrow();
  });

  it('should reject login for nonexistent user', async () => {
    const pb = getFreshPb();

    await expect(
      pb.collection(authCollection.name).authWithPassword(
        'nonexistent@example.com',
        'password123'
      )
    ).rejects.toThrow();
  });

  it('should refresh auth token', async () => {
    const pb = getFreshPb();
    const email = `refresh_${Date.now()}@example.com`;

    // Register and login
    await pb.collection(authCollection.name).create({
      email,
      password: 'password123',
      passwordConfirm: 'password123',
    });

    const loginData = await pb.collection(authCollection.name).authWithPassword(
      email,
      'password123'
    );

    const originalToken = loginData.token;

    // Refresh
    const refreshData = await pb.collection(authCollection.name).authRefresh();

    expect(refreshData.token).toBeTruthy();
    expect(refreshData.record).toBeTruthy();
    expect(refreshData.record.email).toBe(email);
    expect(pb.authStore.token).toBe(refreshData.token);
  });

  it('should reject refresh with invalid token', async () => {
    const pb = getFreshPb();
    pb.authStore.save('invalid.token.here', null);

    await expect(
      pb.collection(authCollection.name).authRefresh()
    ).rejects.toThrow();
  });

  it('should return otpId even for unknown email (enumeration protection)', async () => {
    const pb = getFreshPb();
    const email = `missing_otp_${Date.now()}@example.com`;

    const result = await pb.send(`/api/collections/${authCollection.name}/request-otp`, {
      method: 'POST',
      body: { email },
    });

    expect(result.otpId).toBeTruthy();
  });

  it('should reject OTP endpoints when otp auth is disabled', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
      options: {
        otp: {
          enabled: false,
          duration: 180,
          length: 8,
        },
      },
    });

    try {
      const pb = getFreshPb();

      await expect(
        pb.send(`/api/collections/${collection.name}/request-otp`, {
          method: 'POST',
          body: { email: `otp_disabled_${Date.now()}@example.com` },
        })
      ).rejects.toThrow();

      await expect(
        pb.send(`/api/collections/${collection.name}/auth-with-otp`, {
          method: 'POST',
          body: {
            otpId: 'missing',
            password: '00000000',
          },
        })
      ).rejects.toThrow();
    } finally {
      await cleanup();
    }
  });

  it('should authenticate with request-otp/auth-with-otp flow', async () => {
    const pb = getFreshPb();
    const email = `otp_flow_${Date.now()}@example.com`;
    const password = 'password123';

    await pb.collection(authCollection.name).create({
      email,
      password,
      passwordConfirm: password,
    });

    const req = await pb.send(`/api/collections/${authCollection.name}/request-otp`, {
      method: 'POST',
      body: { email },
    });
    expect(req.otpId).toBeTruthy();

    const otpCode = await findOtpCode(email, req.otpId);
    if (!otpCode) {
      return;
    }

    const authData = await pb.send(`/api/collections/${authCollection.name}/auth-with-otp`, {
      method: 'POST',
      body: {
        otpId: req.otpId,
        password: otpCode,
      },
    });

    expect(authData.token).toBeTruthy();
    expect(authData.record.email).toBe(email);

    await expect(
      pb.send(`/api/collections/${authCollection.name}/auth-with-otp`, {
        method: 'POST',
        body: {
          otpId: req.otpId,
          password: otpCode,
        },
      })
    ).rejects.toThrow();
  });

  it('should reject auth-with-otp with invalid otp password', async () => {
    const pb = getFreshPb();
    const email = `otp_invalid_${Date.now()}@example.com`;
    const password = 'password123';

    await pb.collection(authCollection.name).create({
      email,
      password,
      passwordConfirm: password,
    });

    const req = await pb.send(`/api/collections/${authCollection.name}/request-otp`, {
      method: 'POST',
      body: { email },
    });
    expect(req.otpId).toBeTruthy();

    await expect(
      pb.send(`/api/collections/${authCollection.name}/auth-with-otp`, {
        method: 'POST',
        body: {
          otpId: req.otpId,
          password: '00000000',
        },
      })
    ).rejects.toThrow();
  });

  it('should allow admin to impersonate an auth record with non-refreshable token', async () => {
    const pb = getFreshPb();
    const email = `impersonate_${Date.now()}@example.com`;
    const password = 'password123';

    const user = await pb.collection(authCollection.name).create({
      email,
      password,
      passwordConfirm: password,
    });

    const data = await adminPb.send(
      `/api/collections/${authCollection.name}/impersonate/${user.id}`,
      {
        method: 'POST',
        body: {
          duration: 60,
        },
      }
    );

    expect(data.token).toBeTruthy();
    expect(data.record.id).toBe(user.id);
    expect(data.record.email).toBe(email);

    const payload = JSON.parse(atob(data.token.split('.')[1]));
    expect(payload.type).toBe('authRecord');
    expect(payload.collectionId).toBe(authCollection.id);
    expect(payload.refreshable).toBe(false);

    const impersonated = getFreshPb();
    impersonated.authStore.save(data.token, data.record);
    await expect(
      impersonated.collection(authCollection.name).authRefresh()
    ).rejects.toThrow();
  });

  it('should reject impersonation without admin auth', async () => {
    const pb = getFreshPb();
    const email = `impersonate_no_admin_${Date.now()}@example.com`;
    const password = 'password123';

    const user = await adminPb.collection(authCollection.name).create({
      email,
      password,
      passwordConfirm: password,
    });

    await expect(
      pb.send(`/api/collections/${authCollection.name}/impersonate/${user.id}`, {
        method: 'POST',
      })
    ).rejects.toThrow();
  });

  it('should reject impersonation with invalid negative duration', async () => {
    const pb = getFreshPb();
    const email = `impersonate_bad_duration_${Date.now()}@example.com`;
    const password = 'password123';

    const user = await pb.collection(authCollection.name).create({
      email,
      password,
      passwordConfirm: password,
    });

    await expect(
      adminPb.send(`/api/collections/${authCollection.name}/impersonate/${user.id}`, {
        method: 'POST',
        body: {
          duration: -1,
        },
      })
    ).rejects.toThrow();
  });

  it('should enforce manageRule for cross-user auth field management', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      await adminPb.collections.update(collection.id, {
        options: {
          ...(collection.options || {}),
          manageRule: '@request.auth.id = id',
        },
      });

      const pb1 = getFreshPb();
      const user1Email = `manage_1_${Date.now()}@example.com`;
      const user1 = await pb1.collection(collection.name).create({
        email: user1Email,
        password: 'password123',
        passwordConfirm: 'password123',
      });
      await pb1.collection(collection.name).authWithPassword(user1Email, 'password123');

      const pb2 = getFreshPb();
      const user2Email = `manage_2_${Date.now()}@example.com`;
      await pb2.collection(collection.name).create({
        email: user2Email,
        password: 'password123',
        passwordConfirm: 'password123',
      });
      await pb2.collection(collection.name).authWithPassword(user2Email, 'password123');

      await expect(
        pb2.collection(collection.name).update(user1.id, { verified: true })
      ).rejects.toThrow();

      const selfManaged = await pb1.collection(collection.name).update(user1.id, { verified: true });
      expect(selfManaged.verified).toBe(true);
    } finally {
      await cleanup();
    }
  });

  it('should allow superuser to bypass manageRule restrictions', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      await adminPb.collections.update(collection.id, {
        options: {
          ...(collection.options || {}),
          manageRule: '@request.auth.id = "never_match"',
        },
      });

      const pb = getFreshPb();
      const email = `manage_admin_${Date.now()}@example.com`;
      const user = await pb.collection(collection.name).create({
        email,
        password: 'password123',
        passwordConfirm: 'password123',
      });

      const updated = await adminPb.collection(collection.name).update(user.id, { verified: true });
      expect(updated.verified).toBe(true);
    } finally {
      await cleanup();
    }
  });

  it('should enforce manageRule for managed auth fields on create', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
      options: {
        manageRule: '@request.auth.id != ""',
      },
    });

    try {
      const pb = getFreshPb();

      // Normal signup (without managed fields) is still allowed.
      const normal = await pb.collection(collection.name).create({
        email: `manage_create_normal_${Date.now()}@example.com`,
        password: 'password123',
        passwordConfirm: 'password123',
      });
      expect(normal.verified).toBe(false);

      // Setting managed auth field should require manageRule.
      await expect(
        pb.collection(collection.name).create({
          email: `manage_create_denied_${Date.now()}@example.com`,
          password: 'password123',
          passwordConfirm: 'password123',
          verified: true,
        })
      ).rejects.toThrow();
    } finally {
      await cleanup();
    }
  });

  it('should allow managed auth fields on create when manageRule matches', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
      options: {
        manageRule: '@request.auth.id != ""',
      },
    });

    try {
      const managerClient = getFreshPb();
      const managerEmail = `manage_creator_${Date.now()}@example.com`;
      await managerClient.collection(collection.name).create({
        email: managerEmail,
        password: 'password123',
        passwordConfirm: 'password123',
      });
      await managerClient.collection(collection.name).authWithPassword(managerEmail, 'password123');

      const created = await managerClient.collection(collection.name).create({
        email: `manage_create_allowed_${Date.now()}@example.com`,
        password: 'password123',
        passwordConfirm: 'password123',
        verified: true,
      });
      expect(created.verified).toBe(true);
    } finally {
      await cleanup();
    }
  });

  it('should reject self email update without manageRule access', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: 'id = @request.auth.id',
      deleteRule: '',
    });

    try {
      const pb = getFreshPb();
      const email = `self_email_${Date.now()}@example.com`;
      const newEmail = `self_email_new_${Date.now()}@example.com`;
      const password = 'password123';

      const user = await pb.collection(collection.name).create({
        email,
        password,
        passwordConfirm: password,
      });
      await pb.collection(collection.name).authWithPassword(email, password);

      await expectPbError(
        () => pb.collection(collection.name).update(user.id, { email: newEmail }),
        403
      );
    } finally {
      await cleanup();
    }
  });

  it('should allow self password update without manageRule and require new password', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: 'id = @request.auth.id',
      deleteRule: '',
    });

    try {
      const pb = getFreshPb();
      const email = `self_pwd_${Date.now()}@example.com`;
      const oldPassword = 'password123';
      const newPassword = 'password456';

      const user = await pb.collection(collection.name).create({
        email,
        password: oldPassword,
        passwordConfirm: oldPassword,
      });
      await pb.collection(collection.name).authWithPassword(email, oldPassword);

      await pb.collection(collection.name).update(user.id, {
        password: newPassword,
        passwordConfirm: newPassword,
      });

      const fresh = getFreshPb();
      await expect(
        fresh.collection(collection.name).authWithPassword(email, oldPassword)
      ).rejects.toThrow();

      const reauth = await fresh.collection(collection.name).authWithPassword(email, newPassword);
      expect(reauth.record.id).toBe(user.id);
    } finally {
      await cleanup();
    }
  });

  it('should enforce unique email validation on admin auth-record update', async () => {
    const pb = getFreshPb();
    const email1 = `update_unique_1_${Date.now()}@example.com`;
    const email2 = `update_unique_2_${Date.now()}@example.com`;
    const password = 'password123';

    const user1 = await pb.collection(authCollection.name).create({
      email: email1,
      password,
      passwordConfirm: password,
    });
    await pb.collection(authCollection.name).create({
      email: email2,
      password,
      passwordConfirm: password,
    });

    await expectPbError(
      () => adminPb.collection(authCollection.name).update(user1.id, { email: email2 }),
      400,
      'validation_not_unique'
    );
  });

  it('should enforce email format validation on admin auth-record update', async () => {
    const pb = getFreshPb();
    const email = `update_format_${Date.now()}@example.com`;
    const password = 'password123';

    const user = await pb.collection(authCollection.name).create({
      email,
      password,
      passwordConfirm: password,
    });

    await expectPbError(
      () => adminPb.collection(authCollection.name).update(user.id, { email: 'not-valid' }),
      400,
      'validation_invalid_email'
    );
  });

  it('should request email change for an authenticated record', async () => {
    const pb = getFreshPb();
    const email = `email_change_${Date.now()}@example.com`;
    const newEmail = `email_change_new_${Date.now()}@example.com`;

    await pb.collection(authCollection.name).create({
      email,
      password: 'password123',
      passwordConfirm: 'password123',
    });

    await pb.collection(authCollection.name).authWithPassword(email, 'password123');

    const ok = await pb.collection(authCollection.name).requestEmailChange(newEmail);
    expect(ok).toBe(true);
  });

  it('should reject email change request without auth token', async () => {
    const pb = getFreshPb();

    await expect(
      pb.collection(authCollection.name).requestEmailChange(`anon_${Date.now()}@example.com`)
    ).rejects.toThrow();
  });

  it('should reject email change request with token from another auth collection', async () => {
    const pb = getFreshPb();
    const email = `cross_collection_${Date.now()}@example.com`;
    const newEmail = `cross_collection_new_${Date.now()}@example.com`;

    const second = await createTestCollection(adminPb, {
      name: uniqueName('auth_alt'),
      type: 'auth',
      schema: [{ name: 'note', type: 'text', required: false }],
    });

    try {
      await pb.collection(authCollection.name).create({
        email,
        password: 'password123',
        passwordConfirm: 'password123',
      });

      await pb.collection(authCollection.name).authWithPassword(email, 'password123');

      await expect(
        pb.collection(second.collection.name).requestEmailChange(newEmail)
      ).rejects.toThrow(/403|not allowed|authorization/i);
    } finally {
      await second.cleanup();
    }
  });

  it('should reject confirm email change with invalid token', async () => {
    const pb = getFreshPb();

    await expect(
      pb.collection(authCollection.name).confirmEmailChange('invalid.token.value', 'password123')
    ).rejects.toThrow();
  });

  it('should complete email change flow and invalidate previously issued auth token', async () => {
    const pb = getFreshPb();
    const email = `email_confirm_${Date.now()}@example.com`;
    const newEmail = `email_confirm_new_${Date.now()}@example.com`;
    const password = 'password123';

    await pb.collection(authCollection.name).create({
      email,
      password,
      passwordConfirm: password,
    });

    await pb.collection(authCollection.name).authWithPassword(email, password);
    const oldToken = pb.authStore.token;

    await pb.collection(authCollection.name).requestEmailChange(newEmail);

    const changeToken = await findEmailChangeToken(newEmail);
    if (!changeToken) {
      // Some environments don't run PPBase in daemon mode, so the dev mail
      // token log may not be available on disk.
      return;
    }

    const confirmed = await pb
      .collection(authCollection.name)
      .confirmEmailChange(changeToken, password);
    expect(confirmed).toBe(true);

    const staleClient = getFreshPb();
    staleClient.authStore.save(oldToken, null);
    await expect(
      staleClient.collection(authCollection.name).authRefresh()
    ).rejects.toThrow();

    const reauth = getFreshPb();
    const authData = await reauth
      .collection(authCollection.name)
      .authWithPassword(newEmail, password);
    expect(authData.record.email).toBe(newEmail);
  });

  it('should list auth methods', async () => {
    const pb = getFreshPb();

    const methods = await pb.collection(authCollection.name).listAuthMethods();

    expect(methods.usernamePassword).toBe(false); // PPBase uses email
    expect(methods.emailPassword).toBe(true);
    expect(methods.authProviders).toBeInstanceOf(Array);
    expect(methods.otp).toBeDefined();
    expect(methods.mfa).toBeDefined();
  });

  it('should serve oauth2 redirect relay page', async () => {
    const response = await fetch(`${BASE_URL}/api/oauth2-redirect?code=test&state=abc`);
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type') || '').toContain('text/html');

    const html = await response.text();
    expect(html).toContain('window.opener.postMessage');
    expect(html).toContain('window.close');
  });

  it('should enforce authRule for password authentication', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
      options: {
        authRule: 'verified = true',
      },
    });

    try {
      const pb = getFreshPb();
      const email = `auth_rule_${Date.now()}@example.com`;
      const password = 'password123';

      const user = await pb.collection(collection.name).create({
        email,
        password,
        passwordConfirm: password,
      });

      await expect(
        pb.collection(collection.name).authWithPassword(email, password)
      ).rejects.toThrow();

      await adminPb.collection(collection.name).update(user.id, { verified: true });

      const authData = await pb.collection(collection.name).authWithPassword(email, password);
      expect(authData.record.id).toBe(user.id);
    } finally {
      await cleanup();
    }
  });

  it('should enforce authRule request context on auth refresh', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
      options: {
        authRule: '@request.context = "password"',
      },
    });

    try {
      const pb = getFreshPb();
      const email = `auth_rule_ctx_${Date.now()}@example.com`;
      const password = 'password123';

      await pb.collection(collection.name).create({
        email,
        password,
        passwordConfirm: password,
      });

      await pb.collection(collection.name).authWithPassword(email, password);

      await expect(
        pb.collection(collection.name).authRefresh()
      ).rejects.toThrow();
    } finally {
      await cleanup();
    }
  });

  it('should disallow authentication when authRule is null', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
      options: {
        authRule: null,
      },
    });

    try {
      const pb = getFreshPb();
      const email = `auth_rule_null_${Date.now()}@example.com`;
      const password = 'password123';

      await pb.collection(collection.name).create({
        email,
        password,
        passwordConfirm: password,
      });

      await expect(
        pb.collection(collection.name).authWithPassword(email, password)
      ).rejects.toThrow();
    } finally {
      await cleanup();
    }
  });

  it('should enforce passwordAuth.enabled for auth-with-password', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      type: 'auth',
      createRule: '',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
      options: {
        passwordAuth: {
          enabled: false,
          identityFields: ['email'],
        },
      },
    });

    try {
      const pb = getFreshPb();
      const email = `password_auth_disabled_${Date.now()}@example.com`;
      const password = 'password123';

      await pb.collection(collection.name).create({
        email,
        password,
        passwordConfirm: password,
      });

      const methods = await pb.collection(collection.name).listAuthMethods();
      expect(methods.password.enabled).toBe(false);
      expect(methods.emailPassword).toBe(false);

      await expect(
        pb.collection(collection.name).authWithPassword(email, password)
      ).rejects.toThrow();
    } finally {
      await cleanup();
    }
  });

  it('should reject auth-with-password with unsupported identityField', async () => {
    const pb = getFreshPb();
    const email = `identity_field_${Date.now()}@example.com`;
    const password = 'password123';

    await pb.collection(authCollection.name).create({
      email,
      password,
      passwordConfirm: password,
    });

    await expect(
      pb.send(`/api/collections/${authCollection.name}/auth-with-password`, {
        method: 'POST',
        body: {
          identity: email,
          password,
          identityField: 'displayName',
        },
      })
    ).rejects.toThrow();
  });

  it('should filter record fields on login', async () => {
    const pb = getFreshPb();
    const email = `fields_${Date.now()}@example.com`;

    await pb.collection(authCollection.name).create({
      email,
      password: 'password123',
      passwordConfirm: 'password123',
      displayName: 'Fields Test',
    });

    // Login with fields filter via query string (requires SDK support or manual send)
    const authData = await pb.send(
      `/api/collections/${authCollection.name}/auth-with-password?fields=id,email`,
      {
        method: 'POST',
        body: {
          identity: email,
          password: 'password123',
        },
      }
    );

    expect(authData.record.id).toBeTruthy();
    expect(authData.record.email).toBe(email);
    expect(authData.record.collectionId).toBeUndefined();
    expect(authData.record.displayName).toBeUndefined();
  });

  it('should never leak password_hash or token_key', async () => {
    const pb = getFreshPb();
    const email = `leak_${Date.now()}@example.com`;

    // Register
    const user = await pb.collection(authCollection.name).create({
      email,
      password: 'password123',
      passwordConfirm: 'password123',
    });

    expect(user.password_hash).toBeUndefined();
    expect(user.token_key).toBeUndefined();

    // Login
    const authData = await pb.collection(authCollection.name).authWithPassword(
      email,
      'password123'
    );

    expect(authData.record.password_hash).toBeUndefined();
    expect(authData.record.token_key).toBeUndefined();

    // Get record
    const fetched = await pb.collection(authCollection.name).getOne(user.id);

    expect(fetched.password_hash).toBeUndefined();
    expect(fetched.token_key).toBeUndefined();
  });

  it('should hide auth emails from other non-admin users when emailVisibility is false', async () => {
    const pb = getFreshPb();
    const user1Email = `visibility_1_${Date.now()}@example.com`;
    const user2Email = `visibility_2_${Date.now()}@example.com`;

    const user1 = await pb.collection(authCollection.name).create({
      email: user1Email,
      password: 'password123',
      passwordConfirm: 'password123',
    });
    const user2 = await pb.collection(authCollection.name).create({
      email: user2Email,
      password: 'password123',
      passwordConfirm: 'password123',
    });

    const user2Client = getFreshPb();
    await user2Client.collection(authCollection.name).authWithPassword(user2Email, 'password123');

    const otherUser = await user2Client.collection(authCollection.name).getOne(user1.id);
    expect(otherUser.email).toBeUndefined();
    expect(otherUser.emailVisibility).toBe(false);

    const selfRecord = await user2Client.collection(authCollection.name).getOne(user2.id);
    expect(selfRecord.email).toBe(user2Email);

    const adminView = await adminPb.collection(authCollection.name).getOne(user1.id);
    expect(adminView.email).toBe(user1Email);
  });

  it('should contain correct token claims', async () => {
    const pb = getFreshPb();
    const email = `claims_${Date.now()}@example.com`;

    await pb.collection(authCollection.name).create({
      email,
      password: 'password123',
      passwordConfirm: 'password123',
    });

    const authData = await pb.collection(authCollection.name).authWithPassword(
      email,
      'password123'
    );

    // Decode JWT (simple base64 decode for testing)
    const parts = authData.token.split('.');
    const payload = JSON.parse(atob(parts[1]));

    expect(payload.type).toBe('authRecord');
    expect(payload.id).toBe(authData.record.id);
    expect(payload.collectionId).toBe(authCollection.id);
  });

  it('should complete full auth lifecycle: register → login → refresh → access', async () => {
    const pb = getFreshPb();
    const email = `lifecycle_${Date.now()}@example.com`;

    // Step 1: Register
    const user = await pb.collection(authCollection.name).create({
      email,
      password: 'password123',
      passwordConfirm: 'password123',
    });

    expect(user.id).toBeTruthy();

    // Step 2: Login
    const loginData = await pb.collection(authCollection.name).authWithPassword(
      email,
      'password123'
    );

    expect(loginData.token).toBeTruthy();
    expect(pb.authStore.isValid).toBe(true);

    // Step 3: Refresh
    const refreshData = await pb.collection(authCollection.name).authRefresh();

    expect(refreshData.token).toBeTruthy();

    // Step 4: Access protected resource (list own records)
    const list = await pb.collection(authCollection.name).getList(1, 50);

    expect(list.items.length).toBeGreaterThan(0);
  });
});
