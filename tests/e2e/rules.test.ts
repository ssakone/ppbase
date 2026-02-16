/**
 * PocketBase SDK E2E Tests: API Rules Enforcement
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import type PocketBase from 'pocketbase';
import { getAdminPb, getFreshPb, createTestCollection, registerAndLogin } from './helpers';

describe('API Rules Enforcement', () => {
  let adminPb: PocketBase;

  beforeAll(async () => {
    adminPb = await getAdminPb();
  });

  it('should enforce null listRule as admin-only', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      listRule: null, // admin-only
    });

    try {
      // Unauthenticated list should fail
      const unauthedPb = getFreshPb();
      await expect(
        unauthedPb.collection(collection.name).getList(1, 50)
      ).rejects.toThrow();

      // Admin list should succeed
      const adminList = await adminPb.collection(collection.name).getList(1, 50);
      expect(adminList.items).toBeInstanceOf(Array);
    } finally {
      await cleanup();
    }
  });

  it('should enforce empty string listRule as public', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      listRule: '', // public
    });

    try {
      // Create a record as admin
      await adminPb.collection(collection.name).create({ title: 'Public Record' });

      // Unauthenticated list should succeed
      const unauthedPb = getFreshPb();
      const list = await unauthedPb.collection(collection.name).getList(1, 50);

      expect(list.items.length).toBeGreaterThan(0);
    } finally {
      await cleanup();
    }
  });

  it('should filter records by expression rule', async () => {
    // Create auth collection for users
    const { collection: authColl, cleanup: authCleanup } = await createTestCollection(adminPb, {
      type: 'auth',
    });

    // Create data collection with owner field
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'owner', type: 'relation', required: true, options: { collectionId: authColl.id, maxSelect: 1 } },
      ],
      listRule: 'owner = @request.auth.id', // only own records
      viewRule: 'owner = @request.auth.id',
    });

    try {
      // Register two users
      const pb1 = getFreshPb();
      const { user: user1 } = await registerAndLogin(
        pb1,
        authColl.name,
        `user1_${Date.now()}@example.com`,
        'password123'
      );

      const pb2 = getFreshPb();
      const { user: user2 } = await registerAndLogin(
        pb2,
        authColl.name,
        `user2_${Date.now()}@example.com`,
        'password123'
      );

      // Create records for each user
      await pb1.collection(collection.name).create({
        title: 'User1 Record',
        owner: user1.id,
      });

      await pb2.collection(collection.name).create({
        title: 'User2 Record',
        owner: user2.id,
      });

      // User1 should only see their own record
      const user1List = await pb1.collection(collection.name).getList(1, 50);
      expect(user1List.totalItems).toBe(1);
      expect(user1List.items[0].title).toBe('User1 Record');

      // User2 should only see their own record
      const user2List = await pb2.collection(collection.name).getList(1, 50);
      expect(user2List.totalItems).toBe(1);
      expect(user2List.items[0].title).toBe('User2 Record');
    } finally {
      await cleanup();
      await authCleanup();
    }
  });

  it('should enforce null createRule', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      createRule: null, // admin-only
      listRule: '', // public read
    });

    try {
      const unauthedPb = getFreshPb();

      await expect(
        unauthedPb.collection(collection.name).create({ title: 'Should Fail' })
      ).rejects.toThrow();
    } finally {
      await cleanup();
    }
  });

  it('should enforce expression updateRule', async () => {
    const { collection: authColl, cleanup: authCleanup } = await createTestCollection(adminPb, {
      type: 'auth',
    });

    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'owner', type: 'relation', required: true, options: { collectionId: authColl.id, maxSelect: 1 } },
      ],
      listRule: '',
      createRule: '',
      updateRule: 'owner = @request.auth.id', // only update own
      viewRule: '',
    });

    try {
      const pb1 = getFreshPb();
      const { user: user1 } = await registerAndLogin(
        pb1,
        authColl.name,
        `owner1_${Date.now()}@example.com`,
        'password123'
      );

      const pb2 = getFreshPb();
      const { user: user2 } = await registerAndLogin(
        pb2,
        authColl.name,
        `owner2_${Date.now()}@example.com`,
        'password123'
      );

      // User1 creates a record
      const record = await pb1.collection(collection.name).create({
        title: 'Original',
        owner: user1.id,
      });

      // User1 can update their own record
      const updated = await pb1.collection(collection.name).update(record.id, {
        title: 'Updated by Owner',
      });
      expect(updated.title).toBe('Updated by Owner');

      // User2 cannot update user1's record
      await expect(
        pb2.collection(collection.name).update(record.id, {
          title: 'Hacked by User2',
        })
      ).rejects.toThrow();
    } finally {
      await cleanup();
      await authCleanup();
    }
  });

  it('should enforce expression deleteRule', async () => {
    const { collection: authColl, cleanup: authCleanup } = await createTestCollection(adminPb, {
      type: 'auth',
    });

    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'owner', type: 'relation', required: true, options: { collectionId: authColl.id, maxSelect: 1 } },
      ],
      listRule: '',
      createRule: '',
      deleteRule: 'owner = @request.auth.id',
      viewRule: '',
    });

    try {
      const pb1 = getFreshPb();
      const { user: user1 } = await registerAndLogin(
        pb1,
        authColl.name,
        `del1_${Date.now()}@example.com`,
        'password123'
      );

      const pb2 = getFreshPb();
      await registerAndLogin(
        pb2,
        authColl.name,
        `del2_${Date.now()}@example.com`,
        'password123'
      );

      const record = await pb1.collection(collection.name).create({
        title: 'To Delete',
        owner: user1.id,
      });

      // User2 cannot delete user1's record
      await expect(
        pb2.collection(collection.name).delete(record.id)
      ).rejects.toThrow();

      // User1 can delete their own record
      await pb1.collection(collection.name).delete(record.id);

      await expect(
        pb1.collection(collection.name).getOne(record.id)
      ).rejects.toThrow();
    } finally {
      await cleanup();
      await authCleanup();
    }
  });

  it('should enforce expression viewRule', async () => {
    const { collection: authColl, cleanup: authCleanup } = await createTestCollection(adminPb, {
      type: 'auth',
    });

    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'owner', type: 'relation', required: true, options: { collectionId: authColl.id, maxSelect: 1 } },
      ],
      listRule: '',
      createRule: '',
      viewRule: 'owner = @request.auth.id', // only view own
    });

    try {
      const pb1 = getFreshPb();
      const { user: user1 } = await registerAndLogin(
        pb1,
        authColl.name,
        `view1_${Date.now()}@example.com`,
        'password123'
      );

      const pb2 = getFreshPb();
      await registerAndLogin(
        pb2,
        authColl.name,
        `view2_${Date.now()}@example.com`,
        'password123'
      );

      const record = await pb1.collection(collection.name).create({
        title: 'Private',
        owner: user1.id,
      });

      // User1 can view their own
      const viewed = await pb1.collection(collection.name).getOne(record.id);
      expect(viewed.title).toBe('Private');

      // User2 gets 404 (not 403, to hide existence)
      await expect(
        pb2.collection(collection.name).getOne(record.id)
      ).rejects.toThrow(/requested resource wasn't found/);
    } finally {
      await cleanup();
      await authCleanup();
    }
  });

  it('should allow admin to bypass all rules', async () => {
    const { collection: authColl, cleanup: authCleanup } = await createTestCollection(adminPb, {
      type: 'auth',
    });

    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'owner', type: 'relation', required: true, options: { collectionId: authColl.id, maxSelect: 1 } },
      ],
      listRule: 'owner = @request.auth.id',
      viewRule: 'owner = @request.auth.id',
      updateRule: 'owner = @request.auth.id',
      deleteRule: 'owner = @request.auth.id',
    });

    try {
      const pb = getFreshPb();
      const { user } = await registerAndLogin(
        pb,
        authColl.name,
        `user_${Date.now()}@example.com`,
        'password123'
      );

      // Create record as user
      const record = await pb.collection(collection.name).create({
        title: 'User Record',
        owner: user.id,
      });

      // Admin can see ALL records regardless of rule
      const adminList = await adminPb.collection(collection.name).getList(1, 50);
      expect(adminList.items.length).toBeGreaterThan(0);

      // Admin can view any record
      const adminView = await adminPb.collection(collection.name).getOne(record.id);
      expect(adminView.title).toBe('User Record');

      // Admin can update any record
      await adminPb.collection(collection.name).update(record.id, {
        title: 'Admin Updated',
      });

      // Admin can delete any record
      await adminPb.collection(collection.name).delete(record.id);
    } finally {
      await cleanup();
      await authCleanup();
    }
  });
});
