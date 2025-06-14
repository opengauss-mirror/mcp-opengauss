package org.opengauss.migrationmcp;

import io.restassured.http.Header;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
public class EncryptTest {
    @Autowired
    private Header tokenHeader;

    @Test
    void encryptTest() {
        System.out.println("token header: " + tokenHeader.getValue());
    }
}
