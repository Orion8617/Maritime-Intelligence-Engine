/**
 * Maritime Domain Awareness (MDA) Intelligence Engine
 * Enterprise-grade AIS processing, deterministic simulation, and SNN risk assessment.
 * * Refactored for DRY compliance, algorithmic efficiency, and standard English nomenclature.
 */

import { computeMarineSNN } from './snnRiskEvaluator.js';

// ------------------------------------------------------------------------
// 1. CONSTANTS & CONFIGURATION
// ------------------------------------------------------------------------

export const REGION_BOUNDS = Object.freeze({
  HONDURAS: { minLat: 12.8, maxLat: 17.2, minLon: -90.0, maxLon: -82.5 },
  GULF_OF_MEXICO: { minLat: 24.0, maxLat: 32.0, minLon: -98.0, maxLon: -80.5 }
});

export const VESSEL_TYPES = Object.freeze({
  FISHING: 30, MILITARY: 35, USCG: 55, PASSENGER: 60,
  CARGO: 70, CONTAINER: 71, REEFER: 72, TANKER: 80, TUG: 21
});

// ------------------------------------------------------------------------
// 2. DETERMINISTIC UTILITIES
// ------------------------------------------------------------------------

/**
 * High-performance seeded Pseudo-Random Number Generator (PRNG).
 * @param {number} seed
 * @returns {function(): number} Float between 0 and 1
 */
function createSeededRng(seed) {
  let s = (seed ^ 0x12345678) >>> 0;
  return () => {
    s = Math.imul(s ^ (s >>> 16), 0x45d9f3b);
    s = Math.imul(s ^ (s >>> 16), 0x45d9f3b);
    s = (s ^ (s >>> 16)) >>> 0;
    return s / 0xffffffff;
  };
}

/**
 * Calculates the Haversine distance between two coordinates in kilometers.
 * Optimized for rapid execution in filtering loops.
 */
function calculateHaversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371; // Earth's radius in km
  const dLat = (lat2 - lat1) * (Math.PI / 180);
  const dLon = (lon2 - lon1) * (Math.PI / 180);
  const a = Math.sin(dLat / 2) ** 2 +
            Math.cos(lat1 * (Math.PI / 180)) * Math.cos(lat2 * (Math.PI / 180)) *
            Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function resolveVesselColor(typeCode) {
  switch (typeCode) {
    case VESSEL_TYPES.FISHING: return '#00ff88';
    case VESSEL_TYPES.TUG: return '#ffaa00';
    case VESSEL_TYPES.CONTAINER:
    case VESSEL_TYPES.REEFER: return '#00ccff';
    case VESSEL_TYPES.TANKER: return '#ff9900';
    case VESSEL_TYPES.MILITARY:
    case VESSEL_TYPES.USCG: return '#ff4444';
    default: return typeCode >= 70 && typeCode < 80 ? '#00aaff' : '#aaaaaa';
  }
}

// ------------------------------------------------------------------------
// 3. CORE SIMULATION ENGINE (DRY IMPLEMENTATION)
// ------------------------------------------------------------------------

/**
 * Universal Fleet Simulator. Replaces duplicated Honduras and Gulf logic.
 * Processes deterministic vessel movements based on current time slots.
 */
export function simulateFleetTrajectories(routes, ports, geomagContext, seedOffset = 0, boundingBox = null) {
  const currentSlot = Math.floor(Date.now() / 300000); // 5-minute slots
  const currentHourUTC = new Date().getUTCHours();
  const simulatedVessels = [];

  for (let i = 0; i < routes.length; i++) {
    const route = routes[i];
    const rng = createSeededRng(currentSlot * 10000 + i * 997 + seedOffset);

    const deltaLat = route.eLat - route.sLat;
    const deltaLon = route.eLon - route.sLon;
    const routeDistanceDeg = Math.sqrt(deltaLat ** 2 + deltaLon ** 2);
    const degreesPerSlot = (route.speed * 0.0145) / 12;

    let currentLat, currentLon, trueHeading, courseOverGround;

    if (routeDistanceDeg < 0.05) {
      // Vessel is stationary or loitering
      currentLat = route.sLat + (rng() - 0.5) * 0.12;
      currentLon = route.sLon + (rng() - 0.5) * 0.12;
      trueHeading = Math.round(rng() * 360);
      courseOverGround = trueHeading;
    } else {
      // Vessel is underway
      const totalRequiredSlots = Math.max(1, routeDistanceDeg / degreesPerSlot);
      const voyageProgress = (currentSlot % Math.ceil(totalRequiredSlots)) / Math.ceil(totalRequiredSlots);

      currentLat = route.sLat + deltaLat * voyageProgress + (rng() - 0.5) * 0.015;
      currentLon = route.sLon + deltaLon * voyageProgress + (rng() - 0.5) * 0.015;

      trueHeading = Math.round(((Math.atan2(deltaLon, deltaLat) * (180 / Math.PI)) + 360) % 360);
      courseOverGround = (trueHeading + Math.round((rng() - 0.5) * 10) + 360) % 360;
    }

    currentLat = parseFloat(currentLat.toFixed(4));
    currentLon = parseFloat(currentLon.toFixed(4));

    // Spatial Culling: Drop out-of-bounds vessels immediately to save CPU cycles
    if (boundingBox) {
      if (currentLat < boundingBox.minLat || currentLat > boundingBox.maxLat ||
          currentLon < boundingBox.minLon || currentLon > boundingBox.maxLon) {
        continue;
      }
    }

    const calculatedSpeed = parseFloat((route.speed + (rng() - 0.5) * 0.6).toFixed(1));

    const vesselData = {
      mmsi: route.mmsi,
      name: route.name,
      type: route.type,
      typeLabel: route.typeLabel,
      flag: route.flag,
      length_m: route.length,
      lat: currentLat,
      lon: currentLon,
      speed_knots: calculatedSpeed,
      heading_deg: trueHeading,
      cog_deg: courseOverGround,
      navigation_status: 'UNDERWAY',
      color_hex: resolveVesselColor(route.type),
      data_source: 'AIS_SIM',
      origin: { lat: parseFloat(route.sLat.toFixed(4)), lon: parseFloat(route.sLon.toFixed(4)) },
      destination: { lat: parseFloat(route.eLat.toFixed(4)), lon: parseFloat(route.eLon.toFixed(4)) },
      is_active_route: routeDistanceDeg >= 0.05
    };

    // Integrate SNN Risk Assessment
    const snnEvaluation = computeMarineSNN(vesselData, {
      ports: ports,
      geomag: geomagContext,
      hourUTC: currentHourUTC,
      nearbyVessels: []
    });

    // Translate SNN keys to standard English in the output
    vesselData.snn_risk_score = snnEvaluation.neat;
    vesselData.snn_risk_metrics = {
      kinematic_anomaly: snnEvaluation.cinetico,
      tactical_risk: snnEvaluation.tactico,
      positional_risk: snnEvaluation.posicional,
      historical_deviation: snnEvaluation.historico,
      geomagnetic_anomaly: snnEvaluation.magnetico,
      learning_rate_alpha: snnEvaluation.alpha,
      momentum_beta: snnEvaluation.beta
    };

    simulatedVessels.push(vesselData);
  }

  return { vessels: simulatedVessels, slot: currentSlot, generated_at: new Date().toISOString() };
}

// ------------------------------------------------------------------------
// 4. SENSOR FUSION ENGINE (LIVE + SIM)
// ------------------------------------------------------------------------

/**
 * Fuses live AIS data with SNN-enriched simulated data.
 * Deduplicates vessels using exact MMSI matching and spatial proximity thresholds.
 */
export async function fuseMaritimeIntelligence(regionName, liveAisData, liveGfwData, simulatedFleetData, geomagContext) {
  const currentHourUTC = new Date().getUTCHours();

  const enrichLiveVessel = (vessel) => {
    const snnEvaluation = computeMarineSNN(vessel, { ports: [], geomag: geomagContext, hourUTC: currentHourUTC, nearbyVessels: [] });
    return {
      ...vessel,
      snn_risk_score: snnEvaluation.neat,
      snn_risk_metrics: {
        kinematic_anomaly: snnEvaluation.cinetico,
        tactical_risk: snnEvaluation.tactico,
        positional_risk: snnEvaluation.posicional,
        historical_deviation: snnEvaluation.historico,
        geomagnetic_anomaly: snnEvaluation.magnetico
      }
    };
  };

  // Priority Merge: Live AIS overrides GFW (Global Fishing Watch)
  const liveVesselsMap = new Map();
  for (const v of liveGfwData) liveVesselsMap.set(v.mmsi, enrichLiveVessel(v));
  for (const v of liveAisData) liveVesselsMap.set(v.mmsi, enrichLiveVessel(v));

  const liveVesselsArray = Array.from(liveVesselsMap.values());
  const liveMmsiSet = new Set(liveVesselsArray.map(v => v.mmsi));

  // Intelligent Deduplication:
  // 1. Drop simulated vessels with identical MMSI.
  // 2. Spatial Gap Filling: Drop simulated vessels within 25km of ANY live vessel to prevent ghosting.
  const filteredSimVessels = simulatedFleetData.vessels.filter(simVessel =>
    !liveMmsiSet.has(simVessel.mmsi) &&
    !liveVesselsArray.some(liveVessel => calculateHaversineKm(simVessel.lat, simVessel.lon, liveVessel.lat, liveVessel.lon) < 25)
  );

  const unifiedFleet = [...liveVesselsArray, ...filteredSimVessels];

  return {
    region: regionName,
    vessel_fleet: unifiedFleet,
    metadata: {
      total_vessels: unifiedFleet.length,
      live_ais_count: liveAisData.length,
      live_gfw_count: liveGfwData.length - Array.from(liveMmsiSet).filter(m => liveGfwData.some(g => g.mmsi === m)).length, // Unique GFW
      simulated_count: filteredSimVessels.length,
      geomagnetic_baseline: geomagContext,
      timestamp_utc: new Date().toISOString()
    }
  };
}