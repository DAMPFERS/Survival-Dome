import random
import math

from dataclasses import dataclass


@dataclass
class DustParticle:

    x: float
    y: float

    velocity_x: float
    velocity_y: float

    size: float

    alpha: float

    life: float


class DustStorm:

    def __init__(self, x, y, radius):

        self.x = x
        self.y = y

        self.radius = radius

        self.particles = []

        self.spawn_particles(450)

    def spawn_particles(self, count):

        for _ in range(count):

            angle = random.uniform(0, math.pi * 2)

            distance = random.uniform(0, self.radius)

            px = self.x + math.cos(angle) * distance
            py = self.y + math.sin(angle) * distance

            self.particles.append(
                DustParticle(
                    x=px,
                    y=py,

                    velocity_x=random.uniform(-10, 10),
                    velocity_y=random.uniform(-10, 10),

                    size=random.uniform(2, 8),

                    alpha=random.uniform(20, 80),

                    life=random.uniform(4, 10)
                )
            )

    def update(self, delta_time, wind_field):

        for particle in self.particles:

            wind_x, wind_y = wind_field.sample_vector(
                particle.x,
                particle.y
            )

            particle.velocity_x += wind_x * 0.02
            particle.velocity_y += wind_y * 0.02

            particle.velocity_x *= 0.98
            particle.velocity_y *= 0.98

            particle.x += particle.velocity_x * delta_time
            particle.y += particle.velocity_y * delta_time

            particle.life -= delta_time

            particle.alpha *= 0.995

            if particle.life <= 0:

                particle.x = self.x + random.uniform(
                    -self.radius,
                    self.radius
                )

                particle.y = self.y + random.uniform(
                    -self.radius,
                    self.radius
                )

                particle.life = random.uniform(4, 10)

                particle.alpha = random.uniform(20, 80)