import { handleNewsEventsRequest } from '../../packages/shared/src/news-chart-events'

export const onRequest: PagesFunction = async (context) => {
  if (context.request.method === 'OPTIONS') {
    return new Response(null, { status: 204 })
  }
  if (context.request.method !== 'GET') {
    return Response.json({ error: 'method_not_allowed', events: [] }, { status: 405 })
  }
  return handleNewsEventsRequest(context.request)
}
