/**
 * E2E tests for SSE Realtime functionality.
 *
 * Tests the SSE connection, subscriptions, and event broadcasting
 * for create, update, and delete operations.
 */

// CRITICAL: EventSource polyfill must be loaded BEFORE PocketBase import
// eventsource v4.x exports EventSource as a named export, not default
import * as EventSourceModule from 'eventsource';

// Extract EventSource class from the module
const EventSourceClass = (EventSourceModule as any).EventSource;

// Assign to global scopes for PocketBase SDK compatibility
(globalThis as any).EventSource = EventSourceClass;
(global as any).EventSource = EventSourceClass;

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import PocketBase from 'pocketbase';
import {
  getAdminPb,
  getFreshPb,
  createTestCollection,
  uniqueName,
  waitFor,
} from './helpers';

describe('Realtime SSE', () => {
  let adminPb: PocketBase;
  let clientPb: PocketBase;
  let collectionName: string;
  let cleanup: () => Promise<void>;

  beforeAll(async () => {
    adminPb = await getAdminPb();
    clientPb = getFreshPb();

    // Create a test collection with public rules
    const result = await createTestCollection(adminPb, {
      name: uniqueName('realtime_test'),
      schema: [
        { name: 'title', type: 'text', required: true },
        { name: 'count', type: 'number', required: false },
      ],
      listRule: '',  // Public
      viewRule: '',  // Public
      createRule: '', // Public
      updateRule: '', // Public
      deleteRule: '', // Public
    });

    collectionName = result.collection.name;
    cleanup = result.cleanup;
  });

  afterAll(async () => {
    if (cleanup) {
      await cleanup();
    }
  });

  it('should establish SSE connection and receive PB_CONNECT event', async () => {
    let clientId: string | null = null;

    // Subscribe and wait for PB_CONNECT
    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(() => {
        clientPb.realtime.unsubscribe();
        reject(new Error('Timeout waiting for PB_CONNECT'));
      }, 5000);

      clientPb.realtime.subscribe('PB_CONNECT', (data) => {
        clearTimeout(timeout);
        clientId = data.clientId;
        resolve();
      });
    });

    expect(clientId).toBeTruthy();
    expect(typeof clientId).toBe('string');
    expect(clientId!.length).toBeGreaterThan(0);

    // Cleanup
    clientPb.realtime.unsubscribe();
  });

  it('should receive create event when subscribing to collection/*', async () => {
    const events: any[] = [];
    let recordId: string;

    // Subscribe to collection-wide events
    clientPb.realtime.subscribe(`${collectionName}/*`, (data) => {
      events.push(data);
    });

    // Wait for subscription to be active
    await new Promise(resolve => setTimeout(resolve, 500));

    // Create a record
    const record = await clientPb.collection(collectionName).create({
      title: 'Test Create Event',
      count: 1,
    });
    recordId = record.id;

    // Wait for event
    await waitFor(async () => events.length > 0, 5000);

    expect(events.length).toBeGreaterThan(0);
    const createEvent = events.find(e => e.action === 'create');
    expect(createEvent).toBeDefined();
    expect(createEvent.record).toBeDefined();
    expect(createEvent.record.id).toBe(recordId);
    expect(createEvent.record.title).toBe('Test Create Event');

    // Cleanup
    clientPb.realtime.unsubscribe(`${collectionName}/*`);
    await clientPb.collection(collectionName).delete(recordId);
  });

  it('should receive update event when subscribing to collection/*', async () => {
    const events: any[] = [];

    // Create a record first
    const record = await clientPb.collection(collectionName).create({
      title: 'Test Update Original',
      count: 1,
    });

    // Subscribe to collection-wide events
    clientPb.realtime.subscribe(`${collectionName}/*`, (data) => {
      events.push(data);
    });

    // Wait for subscription to be active
    await new Promise(resolve => setTimeout(resolve, 500));

    // Update the record
    await clientPb.collection(collectionName).update(record.id, {
      title: 'Test Update Modified',
      count: 2,
    });

    // Wait for event
    await waitFor(async () => events.some(e => e.action === 'update'), 5000);

    const updateEvent = events.find(e => e.action === 'update');
    expect(updateEvent).toBeDefined();
    expect(updateEvent.record).toBeDefined();
    expect(updateEvent.record.id).toBe(record.id);
    expect(updateEvent.record.title).toBe('Test Update Modified');
    expect(updateEvent.record.count).toBe(2);

    // Cleanup
    clientPb.realtime.unsubscribe(`${collectionName}/*`);
    await clientPb.collection(collectionName).delete(record.id);
  });

  it('should receive delete event when subscribing to collection/*', async () => {
    const events: any[] = [];

    // Create a record first
    const record = await clientPb.collection(collectionName).create({
      title: 'Test Delete',
      count: 1,
    });

    // Subscribe to collection-wide events
    clientPb.realtime.subscribe(`${collectionName}/*`, (data) => {
      events.push(data);
    });

    // Wait for subscription to be active
    await new Promise(resolve => setTimeout(resolve, 500));

    // Delete the record
    await clientPb.collection(collectionName).delete(record.id);

    // Wait for event
    await waitFor(async () => events.some(e => e.action === 'delete'), 5000);

    const deleteEvent = events.find(e => e.action === 'delete');
    expect(deleteEvent).toBeDefined();
    expect(deleteEvent.record).toBeDefined();
    expect(deleteEvent.record.id).toBe(record.id);

    // Cleanup
    clientPb.realtime.unsubscribe(`${collectionName}/*`);
  });

  it('should receive events only for subscribed single record', async () => {
    const events: any[] = [];

    // Create two records
    const record1 = await clientPb.collection(collectionName).create({
      title: 'Record 1',
      count: 1,
    });
    const record2 = await clientPb.collection(collectionName).create({
      title: 'Record 2',
      count: 2,
    });

    // Subscribe only to record1
    clientPb.realtime.subscribe(`${collectionName}/${record1.id}`, (data) => {
      events.push(data);
    });

    // Wait for subscription to be active
    await new Promise(resolve => setTimeout(resolve, 500));

    // Update record1 (should receive event)
    await clientPb.collection(collectionName).update(record1.id, {
      title: 'Record 1 Updated',
    });

    // Update record2 (should NOT receive event)
    await clientPb.collection(collectionName).update(record2.id, {
      title: 'Record 2 Updated',
    });

    // Wait for events
    await new Promise(resolve => setTimeout(resolve, 1000));

    // Should only have event for record1
    expect(events.length).toBeGreaterThan(0);
    expect(events.every(e => e.record.id === record1.id)).toBe(true);
    expect(events.find(e => e.record.id === record2.id)).toBeUndefined();

    // Cleanup
    clientPb.realtime.unsubscribe(`${collectionName}/${record1.id}`);
    await clientPb.collection(collectionName).delete(record1.id);
    await clientPb.collection(collectionName).delete(record2.id);
  });

  it('should handle multiple subscriptions simultaneously', async () => {
    const collectionEvents: any[] = [];
    const record1Events: any[] = [];

    // Create a record
    const record = await clientPb.collection(collectionName).create({
      title: 'Multi Subscription Test',
      count: 1,
    });

    // Subscribe to both collection/* and specific record
    clientPb.realtime.subscribe(`${collectionName}/*`, (data) => {
      collectionEvents.push(data);
    });

    clientPb.realtime.subscribe(`${collectionName}/${record.id}`, (data) => {
      record1Events.push(data);
    });

    // Wait for subscriptions to be active
    await new Promise(resolve => setTimeout(resolve, 500));

    // Update the record
    await clientPb.collection(collectionName).update(record.id, {
      title: 'Multi Updated',
    });

    // Wait for events
    await waitFor(
      async () => collectionEvents.length > 0 && record1Events.length > 0,
      5000
    );

    // Both subscriptions should receive the event
    expect(collectionEvents.length).toBeGreaterThan(0);
    expect(record1Events.length).toBeGreaterThan(0);

    const collEvent = collectionEvents.find(e => e.action === 'update');
    const recEvent = record1Events.find(e => e.action === 'update');

    expect(collEvent).toBeDefined();
    expect(recEvent).toBeDefined();
    expect(collEvent.record.id).toBe(record.id);
    expect(recEvent.record.id).toBe(record.id);

    // Cleanup
    clientPb.realtime.unsubscribe(`${collectionName}/*`);
    clientPb.realtime.unsubscribe(`${collectionName}/${record.id}`);
    await clientPb.collection(collectionName).delete(record.id);
  });

  it('should stop receiving events after unsubscribe', async () => {
    const events: any[] = [];

    // Create a record
    const record = await clientPb.collection(collectionName).create({
      title: 'Unsubscribe Test',
      count: 1,
    });

    // Subscribe
    clientPb.realtime.subscribe(`${collectionName}/*`, (data) => {
      events.push(data);
    });

    // Wait for subscription
    await new Promise(resolve => setTimeout(resolve, 500));

    // Update (should receive event)
    await clientPb.collection(collectionName).update(record.id, {
      count: 2,
    });

    await waitFor(async () => events.length > 0, 5000);
    const eventsBeforeUnsubscribe = events.length;

    // Unsubscribe
    clientPb.realtime.unsubscribe(`${collectionName}/*`);
    await new Promise(resolve => setTimeout(resolve, 500));

    // Update again (should NOT receive event)
    await clientPb.collection(collectionName).update(record.id, {
      count: 3,
    });

    // Wait to ensure no new events
    await new Promise(resolve => setTimeout(resolve, 1000));

    // Event count should not increase
    expect(events.length).toBe(eventsBeforeUnsubscribe);

    // Cleanup
    await clientPb.collection(collectionName).delete(record.id);
  });

  it('should receive events with correct record data structure', async () => {
    const events: any[] = [];

    // Subscribe
    clientPb.realtime.subscribe(`${collectionName}/*`, (data) => {
      events.push(data);
    });

    await new Promise(resolve => setTimeout(resolve, 500));

    // Create a record
    const record = await clientPb.collection(collectionName).create({
      title: 'Structure Test',
      count: 42,
    });

    await waitFor(async () => events.length > 0, 5000);

    const event = events[0];

    // Verify event structure
    expect(event).toHaveProperty('action');
    expect(event.action).toBe('create');
    expect(event).toHaveProperty('record');

    // Verify record structure
    expect(event.record).toHaveProperty('id');
    expect(event.record).toHaveProperty('collectionId');
    expect(event.record).toHaveProperty('collectionName');
    expect(event.record).toHaveProperty('created');
    expect(event.record).toHaveProperty('updated');
    expect(event.record).toHaveProperty('title');
    expect(event.record).toHaveProperty('count');

    // Verify values
    expect(event.record.id).toBe(record.id);
    expect(event.record.collectionName).toBe(collectionName);
    expect(event.record.title).toBe('Structure Test');
    expect(event.record.count).toBe(42);

    // Cleanup
    clientPb.realtime.unsubscribe(`${collectionName}/*`);
    await clientPb.collection(collectionName).delete(record.id);
  });
});
