/**
 * PocketBase SDK E2E Tests: Collections API
 */
import { describe, it, expect, beforeAll } from 'vitest';
import type PocketBase from 'pocketbase';
import { getAdminPb, getFreshPb, createTestCollection, uniqueName } from './helpers';

describe('Collections API', () => {
  let adminPb: PocketBase;

  beforeAll(async () => {
    adminPb = await getAdminPb();
  });

  it('should create a base collection', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      name: uniqueName('base'),
      type: 'base',
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'count', type: 'number', required: false },
      ],
    });

    try {
      expect(collection.id).toBeTruthy();
      expect(collection.name).toMatch(/^base_/);
      expect(collection.type).toBe('base');
      expect(collection.schema).toHaveLength(2);
      expect(collection.schema[0].name).toBe('title');
    } finally {
      await cleanup();
    }
  });

  it('should create an auth collection', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      name: uniqueName('auth'),
      type: 'auth',
      schema: [{ name: 'displayName', type: 'text', required: false }],
    });

    try {
      expect(collection.type).toBe('auth');
      expect(collection.options).toBeTruthy();
      expect(collection.options.passwordAuth).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  it('should merge custom auth options with generated defaults on create', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      name: uniqueName('auth_opts'),
      type: 'auth',
      options: {
        manageRule: '@request.auth.id != ""',
        authToken: {
          duration: 1234,
        },
      },
    });

    try {
      expect(collection.options.manageRule).toBe('@request.auth.id != ""');
      expect(collection.options.authToken.duration).toBe(1234);
      expect(typeof collection.options.authToken.secret).toBe('string');
      expect(collection.options.authToken.secret.length).toBe(50);
      expect(collection.options.passwordResetToken).toBeTruthy();
      expect(collection.options.verificationToken).toBeTruthy();
      expect(collection.options.fileToken).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  it('should update collection schema (add field)', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb);

    try {
      const updated = await adminPb.collections.update(collection.id, {
        schema: [
          ...collection.schema,
          { name: 'newField', type: 'text', required: false },
        ],
      });

      expect(updated.schema).toHaveLength(collection.schema.length + 1);
      expect(updated.schema[updated.schema.length - 1].name).toBe('newField');
    } finally {
      await cleanup();
    }
  });

  it('should update collection rules', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      listRule: null, // admin-only
    });

    try {
      const updated = await adminPb.collections.update(collection.id, {
        listRule: '', // now public
      });

      expect(updated.listRule).toBe('');
    } finally {
      await cleanup();
    }
  });

  it('should delete a collection', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb);

    await adminPb.collections.delete(collection.id);

    // Verify deletion
    await expect(adminPb.collections.getOne(collection.id)).rejects.toThrow();
  });

  it('should list collections with pagination', async () => {
    const list = await adminPb.collections.getList(1, 10);

    expect(list.page).toBe(1);
    expect(list.perPage).toBe(10);
    expect(list.totalItems).toBeGreaterThan(0);
    expect(list.items).toBeInstanceOf(Array);
  });

  it('should reject collections list with invalid admin token', async () => {
    const pb = getFreshPb();

    await expect(
      pb.send('/api/collections', {
        method: 'GET',
        headers: {
          Authorization: 'invalid.jwt.token',
        },
      })
    ).rejects.toMatchObject({
      status: 401,
    });
  });

  it('should return collection scaffolds metadata', async () => {
    const scaffolds = await adminPb.send('/api/collections/meta/scaffolds', {
      method: 'GET',
    });

    expect(scaffolds.auth?.type).toBe('auth');
    expect(scaffolds.base?.type).toBe('base');
    expect(scaffolds.view?.type).toBe('view');
    expect(scaffolds.auth?.passwordAuth?.enabled).toBe(true);
    expect(scaffolds.auth?.authToken?.duration).toBeGreaterThan(0);
  });

  it('should get single collection by name', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb);

    try {
      const fetched = await adminPb.collections.getOne(collection.name);
      expect(fetched.id).toBe(collection.id);
      expect(fetched.name).toBe(collection.name);
    } finally {
      await cleanup();
    }
  });

  it('should block deletion of system collection', async () => {
    await expect(
      adminPb.collections.delete('_superusers')
    ).rejects.toThrow();
  });

  it('should reject reserved collection name', async () => {
    // 'users' is a reserved name (bootstrapped collection)
    await expect(
      adminPb.collections.create({
        name: 'users',
        type: 'base',
        schema: [],
      })
    ).rejects.toThrow();
  });

  it('should truncate collection (clear all records)', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb);

    try {
      // Create some records
      await adminPb.collection(collection.name).create({ title: 'test1' });
      await adminPb.collection(collection.name).create({ title: 'test2' });

      // Verify records exist
      const before = await adminPb.collection(collection.name).getList(1, 50);
      expect(before.totalItems).toBeGreaterThanOrEqual(2);

      // Truncate via raw send (SDK might not have this method)
      await adminPb.send(`/api/collections/${collection.id}/truncate`, {
        method: 'DELETE',
      });

      // Verify all records cleared
      const after = await adminPb.collection(collection.name).getList(1, 50);
      expect(after.totalItems).toBe(0);
    } finally {
      await cleanup();
    }
  });
});
