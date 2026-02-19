import type { NavigateFunction, NavigateOptions, To } from 'react-router-dom'

interface SmoothNavigateOptions extends NavigateOptions {
  disableTransition?: boolean
}

export function navigateWithTransition(
  navigate: NavigateFunction,
  to: To,
  options: SmoothNavigateOptions = {},
): void {
  const { disableTransition = false, ...navigateOptions } = options
  const startViewTransition =
    typeof document !== 'undefined'
      ? (document as Document & {
          startViewTransition?: (update: () => void) => { finished: Promise<void> }
        }).startViewTransition
      : undefined

  if (
    disableTransition ||
    typeof document === 'undefined' ||
    typeof startViewTransition !== 'function'
  ) {
    navigate(to, navigateOptions)
    return
  }

  try {
    const transition = startViewTransition(() => {
      navigate(to, navigateOptions)
    })
    void transition.finished.catch(() => {
      // Ignore transition completion errors; navigation already happened.
    })
  } catch {
    navigate(to, navigateOptions)
  }
}
