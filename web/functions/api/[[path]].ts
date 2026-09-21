import pagesApp from '../../apps/api/src/pages'
import { handleWorkerProxyRequest } from '../../apps/api/src/worker-proxy'

export const onRequest: PagesFunction<{ WYCKOFF_API_ORIGIN?: string }> = (context) => {
  return handleWorkerProxyRequest(context.request, context.env, (request) => pagesApp.fetch(request, context.env))
}
