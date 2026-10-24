// The settings API serves every non-empty secret as this mask
// (PocketBase 0.22.34 models/settings/settings.go:19-21).
export const SECRET_MASK = '******'

// Returns a deep copy without the props still holding the mask, so that saving
// a form never stores the mask as a secret: the server keeps the stored value
// of an omitted prop. Port of PocketBase 0.22.34
// ui/src/utils/CommonHelper.js:332-349 (filterRedactedProps), which its
// dashboard applies before every settings save.
export function filterRedactedProps<T>(obj: T, mask: string = SECRET_MASK): T {
  const result = JSON.parse(JSON.stringify(obj ?? {}))

  for (const prop in result) {
    if (typeof result[prop] === 'object' && result[prop] !== null) {
      result[prop] = filterRedactedProps(result[prop], mask)
    } else if (result[prop] === mask) {
      delete result[prop]
    }
  }

  return result
}
