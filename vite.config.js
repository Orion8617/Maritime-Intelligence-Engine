import { simulateFleetTrajectories, fuseMaritimeIntelligence, REGION_BOUNDS } from './src/services/MaritimeIntelligenceEngine.js';

// Setup Vite plugin for the dev server to simulate API endpoints
function mdaApiPlugin() {
  return {
    name: 'mda-api-plugin',
    configureServer(server) {
      // -------------------------------------------------------------
      // GULF ROUTE
      // -------------------------------------------------------------
      server.middlewares.use('/api/ais/gulf', async (req, res) => {
        try {
          // 1. Obtener contexto geomagnético (ya lo tienes en tu código)
          const geomagCtx = {}; // await fetchGeomagHN(); // O el específico del Golfo

          // 2. Simular la flota base pasando las rutas y el bounding box
          const simFleet = simulateFleetTrajectories(
            [], // GULF_VESSEL_ROUTES
            [], // GULF_PORTS_SIM
            geomagCtx,
            88888,
            REGION_BOUNDS.GULF_OF_MEXICO
          );

          // 3. Obtener datos en vivo (si existen)
          const liveAis = []; // await fetchAISHubGulf();
          const liveGfw = []; // Añadir fetchGFW() si aplica al Golfo

          // 4. Motor de Fusión (Deduplicación inteligente)
          const finalData = await fuseMaritimeIntelligence(
            'GULF_OF_MEXICO',
            liveAis,
            liveGfw,
            simFleet,
            geomagCtx
          );

          res.setHeader('Content-Type', 'application/json');
          res.end(JSON.stringify(finalData));
        } catch (err) {
          console.error('[MDA API Error]', err);
          res.statusCode = 502;
          res.end(JSON.stringify({ error: err.message }));
        }
      });

      // -------------------------------------------------------------
      // HONDURAS ROUTE
      // -------------------------------------------------------------
      server.middlewares.use('/api/ais/honduras', async (req, res) => {
        try {
          // 1. Obtener contexto geomagnético (ya lo tienes en tu código)
          const geomagCtx = {}; // await fetchGeomagHN();

          // 2. Simular la flota base pasando las rutas y el bounding box
          const simFleet = simulateFleetTrajectories(
            [], // HONDURAS_VESSEL_ROUTES
            [], // HONDURAS_PORTS_SIM
            geomagCtx,
            88888,
            REGION_BOUNDS.HONDURAS
          );

          // 3. Obtener datos en vivo (si existen)
          const liveAis = [];
          const liveGfw = [];

          // 4. Motor de Fusión (Deduplicación inteligente)
          const finalData = await fuseMaritimeIntelligence(
            'HONDURAS',
            liveAis,
            liveGfw,
            simFleet,
            geomagCtx
          );

          res.setHeader('Content-Type', 'application/json');
          res.end(JSON.stringify(finalData));
        } catch (err) {
          console.error('[MDA API Error]', err);
          res.statusCode = 502;
          res.end(JSON.stringify({ error: err.message }));
        }
      });
    }
  }
}

export default {
  plugins: [mdaApiPlugin()]
};
