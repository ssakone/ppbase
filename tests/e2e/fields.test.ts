/**
 * PocketBase SDK E2E Tests: Field Type Validation
 */
import { describe, it, expect, beforeAll } from 'vitest';
import type PocketBase from 'pocketbase';
import { getAdminPb, createTestCollection } from './helpers';

describe('Field Type Validation', () => {
  let adminPb: PocketBase;

  beforeAll(async () => {
    adminPb = await getAdminPb();
  });

  it('should validate text min/max length', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'title',
          type: 'text',
          required: false,
          options: { min: 5, max: 10 },
        },
      ],
    });

    try {
      // Too short
      await expect(
        adminPb.collection(collection.name).create({ title: 'abc' })
      ).rejects.toThrow();

      // Too long
      await expect(
        adminPb.collection(collection.name).create({ title: 'abcdefghijklmnop' })
      ).rejects.toThrow();

      // Just right
      const valid = await adminPb.collection(collection.name).create({ title: 'abcdef' });
      expect(valid.title).toBe('abcdef');
    } finally {
      await cleanup();
    }
  });

  it('should validate number min/max', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'count',
          type: 'number',
          required: false,
          options: { min: 10, max: 100 },
        },
      ],
    });

    try {
      // Too small
      await expect(
        adminPb.collection(collection.name).create({ count: 5 })
      ).rejects.toThrow();

      // Too large
      await expect(
        adminPb.collection(collection.name).create({ count: 200 })
      ).rejects.toThrow();

      // Valid
      const valid = await adminPb.collection(collection.name).create({ count: 50 });
      expect(valid.count).toBe(50);
    } finally {
      await cleanup();
    }
  });

  it('should enforce onlyInt for number fields', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'count',
          type: 'number',
          required: false,
          options: { onlyInt: true },
        },
      ],
    });

    try {
      // Float not allowed
      await expect(
        adminPb.collection(collection.name).create({ count: 3.14 })
      ).rejects.toThrow();

      // Integer allowed
      const valid = await adminPb.collection(collection.name).create({ count: 42 });
      expect(valid.count).toBe(42);
    } finally {
      await cleanup();
    }
  });

  it('should validate email field format', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'contact',
          type: 'email',
          required: false,
        },
      ],
    });

    try {
      // Invalid email
      await expect(
        adminPb.collection(collection.name).create({ contact: 'not-an-email' })
      ).rejects.toThrow();

      // Valid email
      const valid = await adminPb.collection(collection.name).create({
        contact: 'test@example.com',
      });
      expect(valid.contact).toBe('test@example.com');
    } finally {
      await cleanup();
    }
  });

  it('should validate URL field format', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'website',
          type: 'url',
          required: false,
        },
      ],
    });

    try {
      // Invalid URL
      await expect(
        adminPb.collection(collection.name).create({ website: 'not a url' })
      ).rejects.toThrow();

      // Valid URL
      const valid = await adminPb.collection(collection.name).create({
        website: 'https://example.com',
      });
      expect(valid.website).toBe('https://example.com');
    } finally {
      await cleanup();
    }
  });

  it('should handle bool field', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'active',
          type: 'bool',
          required: false,
        },
      ],
    });

    try {
      const recordTrue = await adminPb.collection(collection.name).create({ active: true });
      expect(recordTrue.active).toBe(true);

      const recordFalse = await adminPb.collection(collection.name).create({ active: false });
      expect(recordFalse.active).toBe(false);
    } finally {
      await cleanup();
    }
  });

  it('should validate select field (single)', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'status',
          type: 'select',
          required: false,
          options: { maxSelect: 1, values: ['draft', 'published', 'archived'] },
        },
      ],
    });

    try {
      // Invalid option
      await expect(
        adminPb.collection(collection.name).create({ status: 'invalid' })
      ).rejects.toThrow();

      // Valid option
      const valid = await adminPb.collection(collection.name).create({ status: 'published' });
      expect(valid.status).toBe('published');
    } finally {
      await cleanup();
    }
  });

  it('should enforce maxSelect on multi-select', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'tags',
          type: 'select',
          required: false,
          options: { maxSelect: 2, values: ['a', 'b', 'c', 'd'] },
        },
      ],
    });

    try {
      // Too many selections
      await expect(
        adminPb.collection(collection.name).create({ tags: ['a', 'b', 'c'] })
      ).rejects.toThrow();

      // Valid selections
      const valid = await adminPb.collection(collection.name).create({ tags: ['a', 'b'] });
      expect(valid.tags).toEqual(['a', 'b']);
    } finally {
      await cleanup();
    }
  });

  it('should validate date field', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'eventDate',
          type: 'date',
          required: false,
        },
      ],
    });

    try {
      // Invalid date
      await expect(
        adminPb.collection(collection.name).create({ eventDate: 'not-a-date' })
      ).rejects.toThrow();

      // Valid ISO date
      const valid = await adminPb.collection(collection.name).create({
        eventDate: '2024-12-25 10:30:00.000Z',
      });
      expect(valid.eventDate).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  it('should handle JSON field', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'metadata',
          type: 'json',
          required: false,
        },
      ],
    });

    try {
      // Object
      const obj = await adminPb.collection(collection.name).create({
        metadata: { key: 'value', nested: { foo: 'bar' } },
      });
      expect(obj.metadata).toEqual({ key: 'value', nested: { foo: 'bar' } });

      // Array
      const arr = await adminPb.collection(collection.name).create({
        metadata: [1, 2, 3],
      });
      expect(arr.metadata).toEqual([1, 2, 3]);

      // Null
      const nul = await adminPb.collection(collection.name).create({
        metadata: null,
      });
      expect(nul.metadata).toBeNull();
    } finally {
      await cleanup();
    }
  });

  it('should enforce required field', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'title',
          type: 'text',
          required: true,
        },
      ],
    });

    try {
      // Missing required field
      await expect(
        adminPb.collection(collection.name).create({})
      ).rejects.toThrow(/title/i);

      // Empty required field
      await expect(
        adminPb.collection(collection.name).create({ title: '' })
      ).rejects.toThrow();

      // Valid required field
      const valid = await adminPb.collection(collection.name).create({ title: 'Valid' });
      expect(valid.title).toBe('Valid');
    } finally {
      await cleanup();
    }
  });

  it('should store file field reference', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [
        {
          name: 'avatar',
          type: 'file',
          required: false,
          options: { maxSelect: 1 },
        },
      ],
    });

    try {
      // File field stores filename string (actual upload is separate)
      const record = await adminPb.collection(collection.name).create({
        avatar: 'avatar123.png',
      });
      expect(record.avatar).toBe('avatar123.png');
    } finally {
      await cleanup();
    }
  });
});
