/**
 * PocketBase SDK E2E Tests: Auth Collection Flows
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import type PocketBase from 'pocketbase';
import { getAdminPb, getFreshPb, createTestCollection, uniqueName } from './helpers';

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

  it('should list auth methods', async () => {
    const pb = getFreshPb();

    const methods = await pb.collection(authCollection.name).listAuthMethods();

    expect(methods.usernamePassword).toBe(false); // PPBase uses email
    expect(methods.emailPassword).toBe(true);
    expect(methods.authProviders).toBeInstanceOf(Array);
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
