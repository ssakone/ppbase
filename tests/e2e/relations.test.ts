/**
 * PocketBase SDK E2E Tests: Relations & Expand
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import type PocketBase from 'pocketbase';
import { getAdminPb, createTestCollection } from './helpers';

describe('Relations & Expand', () => {
  let adminPb: PocketBase;

  beforeAll(async () => {
    adminPb = await getAdminPb();
  });

  it('should expand single relation', async () => {
    // Create author collection
    const { collection: authorColl, cleanup: authorCleanup } = await createTestCollection(adminPb, {
      schema: [{ name: 'name', type: 'text', required: true }],
    });

    // Create posts collection with author relation
    const { collection: postsColl, cleanup: postsCleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        {
          name: 'author',
          type: 'relation',
          required: false,
          options: {
            collectionId: authorColl.id,
            maxSelect: 1,
          },
        },
      ],
    });

    try {
      // Create author
      const author = await adminPb.collection(authorColl.name).create({ name: 'John Doe' });

      // Create post with author
      const post = await adminPb.collection(postsColl.name).create({
        title: 'Test Post',
        author: author.id,
      });

      // Fetch with expand
      const expanded = await adminPb.collection(postsColl.name).getOne(post.id, {
        expand: 'author',
      });

      expect(expanded.expand).toBeTruthy();
      expect(expanded.expand.author).toBeTruthy();
      expect(expanded.expand.author.name).toBe('John Doe');
    } finally {
      await postsCleanup();
      await authorCleanup();
    }
  });

  it('should expand nested relations', async () => {
    // Create company collection
    const { collection: companyColl, cleanup: companyCleanup } = await createTestCollection(adminPb, {
      schema: [{ name: 'name', type: 'text', required: true }],
    });

    // Create author collection with company relation
    const { collection: authorColl, cleanup: authorCleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'name', type: 'text', required: true },
        {
          name: 'company',
          type: 'relation',
          required: false,
          options: { collectionId: companyColl.id, maxSelect: 1 },
        },
      ],
    });

    // Create posts collection
    const { collection: postsColl, cleanup: postsCleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        {
          name: 'author',
          type: 'relation',
          required: false,
          options: { collectionId: authorColl.id, maxSelect: 1 },
        },
      ],
    });

    try {
      const company = await adminPb.collection(companyColl.name).create({ name: 'Acme Corp' });
      const author = await adminPb.collection(authorColl.name).create({
        name: 'Jane Smith',
        company: company.id,
      });
      const post = await adminPb.collection(postsColl.name).create({
        title: 'Nested Test',
        author: author.id,
      });

      // Expand two levels deep
      const expanded = await adminPb.collection(postsColl.name).getOne(post.id, {
        expand: 'author.company',
      });

      expect(expanded.expand.author).toBeTruthy();
      expect(expanded.expand.author.name).toBe('Jane Smith');
      expect(expanded.expand.author.expand.company).toBeTruthy();
      expect(expanded.expand.author.expand.company.name).toBe('Acme Corp');
    } finally {
      await postsCleanup();
      await authorCleanup();
      await companyCleanup();
    }
  });

  it('should expand multi-relation', async () => {
    const { collection: tagsColl, cleanup: tagsCleanup } = await createTestCollection(adminPb, {
      schema: [{ name: 'name', type: 'text', required: true }],
    });

    const { collection: postsColl, cleanup: postsCleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        {
          name: 'tags',
          type: 'relation',
          required: false,
          options: { collectionId: tagsColl.id, maxSelect: 5 },
        },
      ],
    });

    try {
      const tag1 = await adminPb.collection(tagsColl.name).create({ name: 'tech' });
      const tag2 = await adminPb.collection(tagsColl.name).create({ name: 'news' });

      const post = await adminPb.collection(postsColl.name).create({
        title: 'Multi Relation Test',
        tags: [tag1.id, tag2.id],
      });

      const expanded = await adminPb.collection(postsColl.name).getOne(post.id, {
        expand: 'tags',
      });

      expect(expanded.expand.tags).toBeInstanceOf(Array);
      expect(expanded.expand.tags.length).toBe(2);
      expect(expanded.expand.tags[0].name).toBeTruthy();
      expect(expanded.expand.tags[1].name).toBeTruthy();
    } finally {
      await postsCleanup();
      await tagsCleanup();
    }
  });

  it('should expand on list endpoint', async () => {
    const { collection: authorColl, cleanup: authorCleanup } = await createTestCollection(adminPb, {
      schema: [{ name: 'name', type: 'text', required: true }],
    });

    const { collection: postsColl, cleanup: postsCleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        {
          name: 'author',
          type: 'relation',
          required: false,
          options: { collectionId: authorColl.id, maxSelect: 1 },
        },
      ],
    });

    try {
      const author = await adminPb.collection(authorColl.name).create({ name: 'List Author' });

      await adminPb.collection(postsColl.name).create({
        title: 'Post 1',
        author: author.id,
      });

      await adminPb.collection(postsColl.name).create({
        title: 'Post 2',
        author: author.id,
      });

      const list = await adminPb.collection(postsColl.name).getList(1, 50, {
        expand: 'author',
      });

      expect(list.items.length).toBeGreaterThanOrEqual(2);
      list.items.forEach(item => {
        if (item.author) {
          expect(item.expand.author.name).toBe('List Author');
        }
      });
    } finally {
      await postsCleanup();
      await authorCleanup();
    }
  });

  it('should gracefully handle expand on nonexistent field', async () => {
    const { collection, cleanup } = await createTestCollection(adminPb, {
      schema: [{ name: 'title', type: 'text', required: true }],
    });

    try {
      const record = await adminPb.collection(collection.name).create({ title: 'Test' });

      // Expand nonexistent field shouldn't crash
      const fetched = await adminPb.collection(collection.name).getOne(record.id, {
        expand: 'nonexistent',
      });

      expect(fetched.id).toBe(record.id);
      // expand key may not exist or may be empty
    } finally {
      await cleanup();
    }
  });

  it('should cascade delete related records when configured', async () => {
    const { collection: parentColl, cleanup: parentCleanup } = await createTestCollection(adminPb, {
      schema: [{ name: 'name', type: 'text', required: true }],
    });

    const { collection: childColl, cleanup: childCleanup } = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        {
          name: 'parent',
          type: 'relation',
          required: false,
          options: {
            collectionId: parentColl.id,
            maxSelect: 1,
            cascadeDelete: true, // Delete child when parent deleted
          },
        },
      ],
    });

    try {
      const parent = await adminPb.collection(parentColl.name).create({ name: 'Parent' });

      const child = await adminPb.collection(childColl.name).create({
        title: 'Child',
        parent: parent.id,
      });

      // Delete parent
      await adminPb.collection(parentColl.name).delete(parent.id);

      // Child should also be deleted (cascade)
      await expect(
        adminPb.collection(childColl.name).getOne(child.id)
      ).rejects.toThrow();
    } finally {
      await childCleanup();
      await parentCleanup();
    }
  });
});
