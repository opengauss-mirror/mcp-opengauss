package org.opengauss.migrationmcp.config;

import lombok.Getter;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Configuration;

@Configuration
@Getter
public class ApplicationConfig {
    @Value("${datakit.url}")
    private String serverUrl;

    @Value("${datakit.user}")
    private String username;

    @Value("${datakit.password}")
    private String password;
}
