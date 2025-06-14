package org.opengauss.migrationmcp;

import org.junit.jupiter.api.Test;
import org.opengauss.migrationmcp.service.PortalHostService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
public class PortalHostServiceTest {
    @Autowired
    PortalHostService portalHostService;

    @Test
    void getPortalHostIdsTest() {
        portalHostService.getPortalHostIds().forEach(System.out::println);
    }
}
