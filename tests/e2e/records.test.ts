/**
 * PocketBase SDK E2E Tests: Records API
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import type PocketBase from 'pocketbase';
import { getAdminPb, createTestCollection } from './helpers';

describe('Records API', () => {
  let adminPb: PocketBase;
  let collection: any;
  let cleanup: () => Promise<void>;

  beforeAll(async () => {
    adminPb = await getAdminPb();
    const result = await createTestCollection(adminPb, {
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'count', type: 'number', required: false },
        { name: 'tags', type: 'select', required: false, options: { maxSelect: 3, values: ['a', 'b', 'c', 'd'] } },
      ],
    });
    collection = result.collection;
    cleanup = result.cleanup;
  });

  afterAll(async () => {
    await cleanup();
  });

  it('should create a record', async () => {
    const record = await adminPb.collection(collection.name).create({
      title: 'Test Title',
      count: 42,
    });

    expect(record.id).toBeTruthy();
    expect(record.title).toBe('Test Title');
    expect(record.count).toBe(42);
    expect(record.created).toBeTruthy();
    expect(record.updated).toBeTruthy();
    expect(record.collectionName).toBe(collection.name);
  });

  it('should get record by ID', async () => {
    const created = await adminPb.collection(collection.name).create({
      title: 'Get Test',
      count: 99,
    });

    const fetched = await adminPb.collection(collection.name).getOne(created.id);

    expect(fetched.id).toBe(created.id);
    expect(fetched.title).toBe('Get Test');
    expect(fetched.count).toBe(99);
  });

  it('should update a record', async () => {
    const created = await adminPb.collection(collection.name).create({
      title: 'Original',
      count: 10,
    });

    const updated = await adminPb.collection(collection.name).update(created.id, {
      title: 'Updated',
      count: 20,
    });

    expect(updated.id).toBe(created.id);
    expect(updated.title).toBe('Updated');
    expect(updated.count).toBe(20);
    expect(new Date(updated.updated).getTime()).toBeGreaterThan(
      new Date(created.updated).getTime()
    );
  });

  it('should delete a record', async () => {
    const created = await adminPb.collection(collection.name).create({
      title: 'To Delete',
    });

    await adminPb.collection(collection.name).delete(created.id);

    // Verify deletion
    await expect(
      adminPb.collection(collection.name).getOne(created.id)
    ).rejects.toThrow();
  });

  it('should list records with pagination', async () => {
    // Create multiple records
    for (let i = 0; i < 5; i++) {
      await adminPb.collection(collection.name).create({
        title: `Pagination Test ${i}`,
      });
    }

    const list = await adminPb.collection(collection.name).getList(1, 3);

    expect(list.page).toBe(1);
    expect(list.perPage).toBe(3);
    expect(list.items.length).toBeLessThanOrEqual(3);
    expect(list.totalItems).toBeGreaterThanOrEqual(5);
  });

  it('should filter records', async () => {
    await adminPb.collection(collection.name).create({ title: 'Alpha', count: 1 });
    await adminPb.collection(collection.name).create({ title: 'Beta', count: 2 });
    await adminPb.collection(collection.name).create({ title: 'Gamma', count: 3 });

    const filtered = await adminPb.collection(collection.name).getList(1, 50, {
      filter: 'title = "Beta"',
    });

    expect(filtered.items.length).toBe(1);
    expect(filtered.items[0].title).toBe('Beta');
  });

  it('should sort records', async () => {
    await adminPb.collection(collection.name).create({ title: 'C', count: 3 });
    await adminPb.collection(collection.name).create({ title: 'A', count: 1 });
    await adminPb.collection(collection.name).create({ title: 'B', count: 2 });

    const sorted = await adminPb.collection(collection.name).getList(1, 50, {
      sort: 'title',
    });

    const titles = sorted.items.map(r => r.title);
    const hasA = titles.includes('A');
    const hasB = titles.includes('B');
    const hasC = titles.includes('C');

    expect(hasA && hasB && hasC).toBe(true);

    // Check relative order of our test records
    const idxA = titles.indexOf('A');
    const idxB = titles.indexOf('B');
    const idxC = titles.indexOf('C');

    if (idxA >= 0 && idxB >= 0) {
      expect(idxA).toBeLessThan(idxB);
    }
    if (idxB >= 0 && idxC >= 0) {
      expect(idxB).toBeLessThan(idxC);
    }
  });

  it('should filter record fields', async () => {
    const created = await adminPb.collection(collection.name).create({
      title: 'Fields Test',
      count: 123,
    });

    const fetched = await adminPb.collection(collection.name).getOne(created.id, {
      fields: 'id,title',
    });

    expect(fetched.id).toBeTruthy();
    expect(fetched.title).toBe('Fields Test');
    expect(fetched.count).toBeUndefined();
    expect(fetched.collectionId).toBeUndefined();
  });

  it('should get first list item', async () => {
    await adminPb.collection(collection.name).create({ title: 'First Match', count: 777 });

    const record = await adminPb.collection(collection.name).getFirstListItem('count = 777');

    expect(record.title).toBe('First Match');
    expect(record.count).toBe(777);
  });

  it('should perform partial update', async () => {
    const created = await adminPb.collection(collection.name).create({
      title: 'Original Title',
      count: 100,
    });

    // Only update count
    const updated = await adminPb.collection(collection.name).update(created.id, {
      count: 200,
    });

    expect(updated.title).toBe('Original Title'); // unchanged
    expect(updated.count).toBe(200); // changed
  });

  it('should append to multi-select field with + modifier', async () => {
    const created = await adminPb.collection(collection.name).create({
      title: 'Modifier Test',
      tags: ['a'],
    });

    const updated = await adminPb.collection(collection.name).update(created.id, {
      'tags+': 'b',
    });

    expect(updated.tags).toContain('a');
    expect(updated.tags).toContain('b');
  });

  it('should remove from multi-select field with - modifier', async () => {
    const created = await adminPb.collection(collection.name).create({
      title: 'Modifier Test 2',
      tags: ['a', 'b', 'c'],
    });

    const updated = await adminPb.collection(collection.name).update(created.id, {
      'tags-': 'b',
    });

    expect(updated.tags).toContain('a');
    expect(updated.tags).not.toContain('b');
    expect(updated.tags).toContain('c');
  });

  it('should prepend to multi-select field with +field modifier', async () => {
    const created = await adminPb.collection(collection.name).create({
      title: 'Modifier Prepend Test',
      tags: ['b', 'c'],
    });

    const updated = await adminPb.collection(collection.name).update(created.id, {
      '+tags': 'a',
    });

    expect(updated.tags).toEqual(['a', 'b', 'c']);
  });
});
