import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.180.0/build/three.module.js';

// =====================================================
// Scene
// =====================================================
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x000000);

// =====================================================
// Camera
// =====================================================
const camera = new THREE.PerspectiveCamera(
    45,
    window.innerWidth / window.innerHeight,
    0.1,
    1000
);
camera.position.z = 5;

// =====================================================
// Renderer
// =====================================================
const container = document.getElementById('scene-container');
const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: false
});
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
container.appendChild(renderer.domElement);

// =====================================================
// Adaptive Planet Size (≤ 1/3 экрана)
// =====================================================
function calculatePlanetRadius() {
    const minDimension = Math.min(window.innerWidth, window.innerHeight);
    const targetSize = minDimension / 2;
    
    const fov = camera.fov * (Math.PI / 180);
    const distance = camera.position.z;
    const visibleHeight = 2 * Math.tan(fov / 2) * distance;
    const pixelsPerUnit = window.innerHeight / visibleHeight;
    
    return (targetSize / pixelsPerUnit) / 2;
}

let planetRadius = calculatePlanetRadius();

// =====================================================
// Planet Geometry
// =====================================================
const geometry = new THREE.SphereGeometry(planetRadius, 64, 64);

// =====================================================
// Main Planet (Wireframe)
// =====================================================
const textureLoader = new THREE.TextureLoader();


const earthTexture = textureLoader.load(
    './assets/earth.jpg'
);

const planetMaterial = new THREE.MeshBasicMaterial({
    map: earthTexture,
    color: 0x00ff41,
    wireframe: true,
    transparent: true,
    opacity: 0.85
});
// const planetMaterial = new THREE.MeshStandardMaterial({
//     map: earthTexture,
//     emissiveMap: earthTexture,        // Добавляем текстуру как карту свечения
//     emissive: 0x00ff41,               // Неоновый зелёный
//     emissiveIntensity: 1.5,           // Усиливаем свечение (было 0.2)
//     roughness: 0.4,                   // Уменьшаем (было 0.9)
//     metalness: 0.6,                   // Добавляем металличность
//     toneMapped: false                 // Отключаем тональную карту для яркости
// });


const planet = new THREE.Mesh(geometry, planetMaterial);
scene.add(planet);

// =====================================================
// Inner Glow (внутреннее свечение)
// =====================================================
const innerGlowGeometry = new THREE.SphereGeometry(planetRadius * 0.98, 64, 64);
const innerGlowMaterial = new THREE.MeshBasicMaterial({
    color: 0x00ff41,
    transparent: true,
    opacity: 0.15,
    side: THREE.BackSide
});




const innerGlow = new THREE.Mesh(innerGlowGeometry, innerGlowMaterial);
scene.add(innerGlow);


// =====================================================
// Middle Glow (промежуточный слой для плавного перехода)
// =====================================================
const middleGlowGeometry = new THREE.SphereGeometry(planetRadius * 1.08, 64, 64);
const middleGlowMaterial = new THREE.MeshBasicMaterial({
    color: 0x00ff41,
    transparent: true,
    opacity: 0.12,
    side: THREE.BackSide
});

const middleGlow = new THREE.Mesh(middleGlowGeometry, middleGlowMaterial);
scene.add(middleGlow);



// =====================================================
// Outer Glow (внешнее свечение)
// =====================================================
const outerGlowGeometry = new THREE.SphereGeometry(planetRadius * 1.2, 64, 64);
const outerGlowMaterial = new THREE.MeshBasicMaterial({
    color: 0x00ff41,
    transparent: true,
    opacity: 0.08,
    side: THREE.BackSide
});

const outerGlow = new THREE.Mesh(outerGlowGeometry, outerGlowMaterial);
scene.add(outerGlow);

// =====================================================
// Mouse Control
// =====================================================
let isDragging = false;
let previousMouseX = 0;
let previousMouseY = 0;

renderer.domElement.addEventListener('mousedown', (event) => {
    isDragging = true;
    previousMouseX = event.clientX;
    previousMouseY = event.clientY;
});

window.addEventListener('mouseup', () => {
    isDragging = false;
});

window.addEventListener('mousemove', (event) => {
    if (!isDragging) return;
    
    const deltaX = event.clientX - previousMouseX;
    const deltaY = event.clientY - previousMouseY;
    const speed = 0.01;
    
    planet.rotation.y += deltaX * speed;
    planet.rotation.x += deltaY * speed;
    
    innerGlow.rotation.copy(planet.rotation);
    middleGlow.rotation.copy(planet.rotation);
    outerGlow.rotation.copy(planet.rotation);
    
    previousMouseX = event.clientX;
    previousMouseY = event.clientY;
});

// =====================================================
// Resize
// =====================================================
window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    
    renderer.setSize(window.innerWidth, window.innerHeight);
    
    // Пересчитываем размер планеты
    const newRadius = calculatePlanetRadius();
    const scale = newRadius / planetRadius;
    
    planet.scale.multiplyScalar(scale);
    innerGlow.scale.multiplyScalar(scale);
    outerGlow.scale.multiplyScalar(scale);
    
    planetRadius = newRadius;
});

// =====================================================
// Animation
// =====================================================
function animate() {
    requestAnimationFrame(animate);
    
    // Медленное автоматическое вращение
    planet.rotation.y += 0.0015;
    innerGlow.rotation.copy(planet.rotation);
    middleGlow.rotation.copy(planet.rotation);
    outerGlow.rotation.copy(planet.rotation);
    
    renderer.render(scene, camera);
}

animate();