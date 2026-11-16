import React, { useState } from "react";
import { apiUrl } from "./apiUrl";

const photoCategories = new Set(["attraction", "food", "hotel", "hotel_anchor", "hotel_arrival"]);

export default function PlacePhoto({ item, mapPin = false }) {
  if (!mapPin && !photoCategories.has(item.category)) return null;
  const poiId = item.place?.place_id;
  const name = item.place?.name || item.hotel_name || item.title;
  return <PlacePhotoTile key={poiId} poiId={poiId} name={name} />;
}

function PlacePhotoTile({ poiId, name }) {
  const [failed, setFailed] = useState(false);
  if (!poiId || failed) {
    return <div className="place-photo place-photo-placeholder" aria-hidden="true">
      <span>{name}</span>
    </div>;
  }
  return <img className="place-photo"
    src={apiUrl(`/api/maps/place-photos/${encodeURIComponent(poiId)}`)}
    alt={`${name}的照片`}
    loading="lazy" decoding="async" draggable={false}
    onError={() => setFailed(true)} />;
}
