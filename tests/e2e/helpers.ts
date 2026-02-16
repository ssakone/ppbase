/**
 * Shared test helpers for PocketBase SDK E2E tests.
 */
import PocketBase from 'pocketbase';

const BASE_URL = process.env.PPBASE_URL || 'http://localhost:8090';
const ADMIN_EMAIL = process.env.PPBASE_ADMIN_EMAIL || 'admin@test.com';
const ADMIN_PASSWORD = process.env.PPBASE_ADMIN_PASSWORD || 'adminpass123';

/**
 * Create a fresh PocketBase client (unauthenticated).
 */
export function getFreshPb(): PocketBase {
  return new PocketBase(BASE_URL);
}

/**
 * Create and authenticate an admin PocketBase client.
 * Uses the /api/admins/auth-with-password endpoint (not SDK's collection auth).
 */
export async function getAdminPb(): Promise<PocketBase> {
  const pb = getFreshPb();

  // Use pb.send() to call the admin auth endpoint directly
  const result = await pb.send('/api/admins/auth-with-password', {
    method: 'POST',
    body: {
      identity: ADMIN_EMAIL,
      password: ADMIN_PASSWORD,
    },
  });

  // Manually save admin token to authStore
  pb.authStore.save(result.token, result.admin);

  return pb;
}

/**
 * Generate a unique name with random suffix.
 */
export function uniqueName(prefix: string): string {
  const randomHex = Math.random().toString(16).substring(2, 10);
  return `${prefix}_${randomHex}`;
}

export interface TestCollectionOptions {
  name?: string;
  type?: 'base' | 'auth' | 'view';
  schema?: any[];
  listRule?: string | null;
  viewRule?: string | null;
  createRule?: string | null;
  updateRule?: string | null;
  deleteRule?: string | null;
}

/**
 * Create a test collection and return cleanup function.
 */
export async function createTestCollection(
  adminPb: PocketBase,
  opts: TestCollectionOptions = {}
): Promise<{ collection: any; cleanup: () => Promise<void> }> {
  const name = opts.name || uniqueName('test_coll');

  const collection = await adminPb.collections.create({
    name,
    type: opts.type || 'base',
    schema: opts.schema || [
      { name: 'title', type: 'text', required: false },
    ],
    listRule: opts.listRule === undefined ? '' : opts.listRule,
    viewRule: opts.viewRule === undefined ? '' : opts.viewRule,
    createRule: opts.createRule === undefined ? '' : opts.createRule,
    updateRule: opts.updateRule === undefined ? '' : opts.updateRule,
    deleteRule: opts.deleteRule === undefined ? '' : opts.deleteRule,
  });

  const cleanup = async () => {
    try {
      await adminPb.collections.delete(collection.id);
    } catch (err) {
      // Ignore 404 errors (collection already deleted)
    }
  };

  return { collection, cleanup };
}

/**
 * Register a user on an auth collection and return login info.
 */
export async function registerAndLogin(
  pb: PocketBase,
  collectionName: string,
  email: string,
  password: string
): Promise<{ user: any; token: string }> {
  // Register
  const user = await pb.collection(collectionName).create({
    email,
    password,
    passwordConfirm: password,
  });

  // Login
  const authData = await pb.collection(collectionName).authWithPassword(email, password);

  return {
    user,
    token: authData.token,
  };
}

/**
 * Wait for a condition with timeout.
 */
export async function waitFor(
  condition: () => Promise<boolean>,
  timeoutMs: number = 5000,
  intervalMs: number = 100
): Promise<void> {
  const startTime = Date.now();

  while (Date.now() - startTime < timeoutMs) {
    if (await condition()) {
      return;
    }
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }

  throw new Error(`Timeout waiting for condition after ${timeoutMs}ms`);
}
