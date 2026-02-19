/**
 * PocketBase SDK E2E Tests: API Rules Enforcement
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import type PocketBase from 'pocketbase';
import { getAdminPb, getFreshPb, createTestCollection, registerAndLogin, uniqueName } from './helpers';

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

  it('should evaluate @request.context in list rules for records API', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      listRule: '@request.context = "default"',
      viewRule: '',
      createRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      await adminPb.collection(collection.name).create({ title: 'Context Default' });

      const unauthedPb = getFreshPb();
      const list = await unauthedPb.collection(collection.name).getList(1, 50);
      expect(list.totalItems).toBe(1);
      expect(list.items[0].title).toBe('Context Default');
    } finally {
      await cleanup();
    }
  });

  it('should evaluate @request.method in list rules', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      listRule: '@request.method = "POST"',
      viewRule: '',
      createRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      await adminPb.collection(collection.name).create({ title: 'Method Rule' });

      // List endpoint uses GET, therefore expression should not match.
      const unauthedPb = getFreshPb();
      const list = await unauthedPb.collection(collection.name).getList(1, 50);
      expect(list.totalItems).toBe(0);
      expect(list.items).toEqual([]);
    } finally {
      await cleanup();
    }
  });

  it('should evaluate @request.headers macros with custom headers', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      listRule: '@request.headers.x_test = "allow"',
      viewRule: '',
      createRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      await adminPb.collection(collection.name).create({ title: 'Header Rule' });

      const unauthedPb = getFreshPb();

      const deniedList = await unauthedPb.collection(collection.name).getList(1, 50);
      expect(deniedList.totalItems).toBe(0);
      expect(deniedList.items).toEqual([]);

      const allowedList = await unauthedPb.collection(collection.name).getList(1, 50, {
        headers: { 'X-Test': 'allow' },
      });
      expect(allowedList.totalItems).toBe(1);
      expect(allowedList.items[0].title).toBe('Header Rule');
    } finally {
      await cleanup();
    }
  });

  it('should support datetime macros in list rule expressions', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      listRule: [
        'created >= @todayStart',
        'created <= @todayEnd',
        '@yesterday < @now',
        '@tomorrow > @now',
        '@second >= 0',
        '@second <= 59',
        '@minute >= 0',
        '@minute <= 59',
        '@hour >= 0',
        '@hour <= 23',
        '@weekday >= 0',
        '@weekday <= 6',
        '@day >= 1',
        '@day <= 31',
        '@month >= 1',
        '@month <= 12',
        '@year >= 2000',
      ].join(' && '),
      viewRule: '',
      createRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      await adminPb.collection(collection.name).create({ title: 'Datetime Rule' });

      const unauthedPb = getFreshPb();
      const list = await unauthedPb.collection(collection.name).getList(1, 50);
      expect(list.totalItems).toBe(1);
      expect(list.items[0].title).toBe('Datetime Rule');
    } finally {
      await cleanup();
    }
  });

  it('should enforce @request.body.*:isset in createRule', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'role', type: 'text', required: false },
      ],
      createRule: '@request.body.role:isset = false',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      const pb = getFreshPb();

      // Missing role is allowed.
      const created = await pb.collection(collection.name).create({ title: 'No Role' });
      expect(created.title).toBe('No Role');

      // Sending role should be rejected by rule.
      await expect(
        pb.collection(collection.name).create({
          title: 'Has Role',
          role: 'admin',
        })
      ).rejects.toThrow();
    } finally {
      await cleanup();
    }
  });

  it('should enforce @request.body.*:changed in updateRule', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [{ name: 'title', type: 'text', required: true }],
      createRule: '',
      // Disallow changing title, but allow resubmitting same value.
      updateRule: '@request.body.title:changed = false',
      listRule: '',
      viewRule: '',
      deleteRule: '',
    });

    try {
      const pb = getFreshPb();
      const record = await pb.collection(collection.name).create({ title: 'Original' });

      const same = await pb.collection(collection.name).update(record.id, { title: 'Original' });
      expect(same.title).toBe('Original');

      await expect(
        pb.collection(collection.name).update(record.id, { title: 'Changed' })
      ).rejects.toThrow();
    } finally {
      await cleanup();
    }
  });

  it('should enforce @request.body.*:length in createRule', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        {
          name: 'tags',
          type: 'select',
          required: false,
          options: { maxSelect: 3, values: ['a', 'b', 'c'] },
        },
      ],
      createRule: '@request.body.tags:length >= 2',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      const pb = getFreshPb();

      await expect(
        pb.collection(collection.name).create({ title: 'Too Short', tags: ['a'] })
      ).rejects.toThrow();

      const ok = await pb.collection(collection.name).create({
        title: 'Enough Tags',
        tags: ['a', 'b'],
      });
      expect(ok.title).toBe('Enough Tags');
    } finally {
      await cleanup();
    }
  });

  it('should enforce field:length in listRule for array-like fields', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        {
          name: 'tags',
          type: 'select',
          required: false,
          options: { maxSelect: 3, values: ['x', 'y', 'z'] },
        },
      ],
      listRule: 'tags:length >= 2',
      viewRule: '',
      createRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      await adminPb.collection(collection.name).create({
        title: 'One Tag',
        tags: ['x'],
      });
      await adminPb.collection(collection.name).create({
        title: 'Two Tags',
        tags: ['x', 'y'],
      });

      const pb = getFreshPb();
      const list = await pb.collection(collection.name).getList(1, 50);
      expect(list.totalItems).toBe(1);
      expect(list.items[0].title).toBe('Two Tags');
    } finally {
      await cleanup();
    }
  });

  it('should enforce @request.body.*:lower in createRule', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [{ name: 'email', type: 'text', required: true }],
      createRule: '@request.body.email:lower = "admin@example.com"',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      const pb = getFreshPb();

      const ok = await pb.collection(collection.name).create({
        email: 'Admin@Example.com',
      });
      expect(ok.email).toBe('Admin@Example.com');

      await expect(
        pb.collection(collection.name).create({
          email: 'user@example.com',
        })
      ).rejects.toThrow();
    } finally {
      await cleanup();
    }
  });

  it('should enforce field:lower in listRule', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [{ name: 'title', type: 'text', required: true }],
      listRule: 'title:lower = "hello world"',
      viewRule: '',
      createRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      await adminPb.collection(collection.name).create({ title: 'HELLO WORLD' });
      await adminPb.collection(collection.name).create({ title: 'Different' });

      const pb = getFreshPb();
      const list = await pb.collection(collection.name).getList(1, 50);
      expect(list.totalItems).toBe(1);
      expect(list.items[0].title).toBe('HELLO WORLD');
    } finally {
      await cleanup();
    }
  });

  it('should enforce @request.body.*:each in createRule', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        {
          name: 'tags',
          type: 'select',
          required: false,
          options: { maxSelect: 3, values: ['pb_create', 'pb_read', 'other'] },
        },
      ],
      createRule: '@request.body.tags:each ~ "pb_"',
      listRule: '',
      viewRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      const pb = getFreshPb();

      const ok = await pb.collection(collection.name).create({
        title: 'Only Prefixed',
        tags: ['pb_create', 'pb_read'],
      });
      expect(ok.title).toBe('Only Prefixed');

      await expect(
        pb.collection(collection.name).create({
          title: 'Mixed',
          tags: ['pb_create', 'other'],
        })
      ).rejects.toThrow();
    } finally {
      await cleanup();
    }
  });

  it('should enforce field:each in listRule', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        {
          name: 'tags',
          type: 'select',
          required: false,
          options: { maxSelect: 3, values: ['pb_a', 'pb_b', 'other'] },
        },
      ],
      listRule: 'tags:each ~ "pb_"',
      viewRule: '',
      createRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      await adminPb.collection(collection.name).create({
        title: 'All Prefixed',
        tags: ['pb_a', 'pb_b'],
      });
      await adminPb.collection(collection.name).create({
        title: 'Has Other',
        tags: ['pb_a', 'other'],
      });

      const pb = getFreshPb();
      const list = await pb.collection(collection.name).getList(1, 50);
      expect(list.totalItems).toBe(1);
      expect(list.items[0].title).toBe('All Prefixed');
    } finally {
      await cleanup();
    }
  });

  it('should support @collection aliases in listRule', async () => {
    const refs = await createTestCollection(adminPb, {
      name: uniqueName('refs'),
      schema: [
        { name: 'linkKey', type: 'text', required: true },
        { name: 'tag', type: 'text', required: true },
      ],
      listRule: '',
      viewRule: '',
      createRule: '',
      updateRule: '',
      deleteRule: '',
    });

    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'itemKey', type: 'text', required: true },
      ],
      listRule: [
        `@collection.${refs.collection.name}:first.linkKey = itemKey`,
        `@collection.${refs.collection.name}:first.tag = "a"`,
        `@collection.${refs.collection.name}:second.linkKey = itemKey`,
        `@collection.${refs.collection.name}:second.tag = "b"`,
      ].join(' && '),
      viewRule: '',
      createRule: '',
      updateRule: '',
      deleteRule: '',
    });

    try {
      await adminPb.collection(refs.collection.name).create({ linkKey: 'k1', tag: 'a' });
      await adminPb.collection(refs.collection.name).create({ linkKey: 'k1', tag: 'b' });
      await adminPb.collection(refs.collection.name).create({ linkKey: 'k2', tag: 'a' });

      await adminPb.collection(collection.name).create({ title: 'Both Tags', itemKey: 'k1' });
      await adminPb.collection(collection.name).create({ title: 'Only A', itemKey: 'k2' });

      const pb = getFreshPb();
      const list = await pb.collection(collection.name).getList(1, 50);
      expect(list.totalItems).toBe(1);
      expect(list.items[0].title).toBe('Both Tags');
    } finally {
      await cleanup();
      await refs.cleanup();
    }
  });
});
