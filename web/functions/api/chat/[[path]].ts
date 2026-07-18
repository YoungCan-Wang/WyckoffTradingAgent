import app from '../../../apps/api/src/pages'

export const onRequest: PagesFunction = (context) => {
  return app.fetch(context.request, context.env)
}
