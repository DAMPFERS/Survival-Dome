import math
import random

from dataclasses import dataclass


@dataclass
class WindParticle:
    x: float
    y: float

    velocity_x: float
    velocity_y: float

    life: float


class WindField:

    def __init__(self, width, height, particle_count=1200):

        self.width = width
        self.height = height

        self.particles = []

        for _ in range(particle_count):

            self.particles.append(
                WindParticle(
                    x=random.uniform(0, width),
                    y=random.uniform(0, height),

                    velocity_x=0,
                    velocity_y=0,

                    life=random.uniform(2, 8)
                )
            )

        self.time = 0.0

    def sample_vector(self, x, y):

        scale = 0.002

        angle = (
            math.sin(x * scale + self.time * 0.3) +
            math.cos(y * scale + self.time * 0.2)
        ) * math.pi

        force = 35

        vx = math.cos(angle) * force
        vy = math.sin(angle) * force

        return vx, vy

    def update(self, delta_time):

        self.time += delta_time

        for particle in self.particles:

            vx, vy = self.sample_vector(
                particle.x,
                particle.y
            )

            particle.velocity_x = vx
            particle.velocity_y = vy

            particle.x += vx * delta_time
            particle.y += vy * delta_time

            particle.life -= delta_time

            if (
                particle.x < 0 or
                particle.x > self.width or
                particle.y < 0 or
                particle.y > self.height or
                particle.life <= 0
            ):

                particle.x = random.uniform(0, self.width)
                particle.y = random.uniform(0, self.height)

                particle.life = random.uniform(2, 8)