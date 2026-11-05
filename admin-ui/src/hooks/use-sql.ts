import { useMutation, useQueryClient } from '@tanstack/react-query'
import { executeSql, type SqlExecuteParams } from '@/api/endpoints/sql'

export function useExecuteSql() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: SqlExecuteParams) => executeSql(params),
    onSuccess: (result) => {
      // A committed script may have changed records, collections or tables.
      if (result.committed) {
        queryClient.invalidateQueries()
      }
    },
  })
}
